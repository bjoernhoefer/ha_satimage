#!/usr/bin/env python3
"""Check, configure and roll out a single host Compose service safely.

Requires Python 3 and PyYAML on the host. Run as a Docker-authorized user;
use sudo for root-owned Compose files. No boot files or runner privileges
are changed. Check-only is the default.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required. On Raspberry Pi OS: sudo apt install python3-yaml")


MEMORY = 768 * 1024 * 1024
CPUS = 1.5
PIDS = 128
LIMITS = {
    "mem_limit": MEMORY,
    "memswap_limit": MEMORY,
    "cpus": CPUS,
    "pids_limit": PIDS,
}


def command(*args, timeout=60):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Compose errors can include interpolated configuration/secrets.
        raise RuntimeError(f"{args[0]} {args[1]} failed (exit {result.returncode}); "
                           "inspect its output locally, without sharing credentials")
    return result.stdout


def compose(file, *args):
    return command(
        "docker", "compose", "--project-directory", str(file.parent),
        "-f", str(file), *args, timeout=600,
    )


def host_check():
    info = json.loads(command("docker", "info", "--format", "{{json .}}"))
    for capability in ("MemoryLimit", "SwapLimit", "PidsLimit", "CpuCfsQuota"):
        if not info.get(capability):
            raise RuntimeError(
                f"Docker reports {capability}=false. Fix host cgroup/kernel support "
                "before applying changes or starting the workload; see README."
            )
    controllers = Path("/sys/fs/cgroup/cgroup.controllers")
    if not controllers.is_file() or not {"memory", "cpu", "pids"} <= set(
        controllers.read_text().split()
    ):
        raise RuntimeError(
            "This script requires cgroup v2 with memory, cpu and pids controllers "
            "at /sys/fs/cgroup. Legacy/custom mounts need manual verification."
        )
    print("Host: Docker memory/swap, CPU and PID support available (cgroup v2).")


def effective(file, service):
    config = json.loads(compose(file, "config", "--format", "json"))
    if service not in config.get("services", {}):
        raise RuntimeError(f"Service {service!r} not found in the selected Compose file")
    return config


def check_compose_files(file, service):
    containers = compose(file, "ps", "-a", "-q", service).split()
    if len(containers) > 1:
        raise RuntimeError("Expected one container; scaled services need manual setup.")
    if containers:
        details = json.loads(command("docker", "inspect", containers[0]))[0]
        labels = details.get("Config", {}).get("Labels") or {}
        files = labels.get("com.docker.compose.project.config_files", "").split(",")
        if files != [str(file)]:
            raise RuntimeError(
                "Existing container uses different/multiple Compose files. "
                "Use its exact host configuration; this script supports one file only."
            )


def check_limits(service):
    if any(float(service.get(key, 0)) != value for key, value in LIMITS.items()):
        raise RuntimeError("Effective Compose limits differ from 768 MiB RAM, "
                           "no extra swap, 1.5 CPUs and 128 PIDs. Run --apply.")
    deploy = service.get("deploy", {}).get("resources", {}).get("limits", {})
    for key, expected in (("memory", MEMORY), ("cpus", CPUS), ("pids", PIDS)):
        if key in deploy and float(deploy[key]) != expected:
            raise RuntimeError(f"Conflicting deploy.resources.limits.{key}")


def configured_document(document, service):
    result = copy.deepcopy(document)
    settings = result["services"][service]
    settings.update(LIMITS)
    deploy = settings.get("deploy", {}).get("resources", {}).get("limits", {})
    for key, value in (("memory", MEMORY), ("cpus", str(CPUS)), ("pids", PIDS)):
        if key in deploy:
            deploy[key] = value
    return result


def without_limits(config, service):
    result = copy.deepcopy(config)
    settings = result["services"][service]
    for key in LIMITS:
        settings.pop(key, None)
    deploy = settings.get("deploy", {}).get("resources", {}).get("limits", {})
    for key in ("memory", "cpus", "pids"):
        deploy.pop(key, None)
    return result


def apply_limits(file, service, before):
    document = yaml.safe_load(file.read_text())
    if service not in document.get("services", {}):
        raise RuntimeError("Service is inherited/included; edit its defining file manually.")
    updated = configured_document(document, service)
    original = file.stat()
    fd, name = tempfile.mkstemp(prefix=".pi-setup-", suffix=".yaml", dir=file.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            yaml.safe_dump(updated, stream, sort_keys=False)
        after = effective(temporary, service)
        check_limits(after["services"][service])
        if without_limits(before, service) != without_limits(after, service):
            raise RuntimeError("YAML rewrite changed unrelated effective configuration; "
                               "original untouched. Edit limits manually instead.")
        if document == updated:
            print("Compose already contains the requested limits; no file changed.")
            return
        backup = file.with_name(file.name + ".backup-" + uuid.uuid4().hex)
        # Backups can contain credentials; never make them world-readable.
        with open(backup, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            stream.write(file.read_text())
        os.chown(temporary, original.st_uid, original.st_gid)
        temporary.chmod(original.st_mode & 0o777)
        os.replace(temporary, file)
        print(f"Limits saved to {file}; private backup: {backup}")
    finally:
        temporary.unlink(missing_ok=True)


def verify_container(container):
    details = json.loads(command("docker", "inspect", container))[0]
    state = details["State"]
    if not state.get("Running"):
        raise RuntimeError("Container is stopped: runtime limits cannot yet be verified.")
    pid = int(state["Pid"])
    paths = [line.split("::", 1)[1] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
             if line.startswith("0::")]
    if len(paths) != 1 or ".." in Path(paths[0]).parts:
        raise RuntimeError("Cannot identify the container's cgroup v2 path")
    root = Path("/sys/fs/cgroup") / paths[0].lstrip("/")
    actual = {key: (root / key).read_text().strip() for key in (
        "memory.max", "memory.swap.max", "pids.max", "cpu.max",
    )}
    if any(actual[key] != str(value) for key, value in (
        ("memory.max", MEMORY), ("memory.swap.max", 0), ("pids.max", PIDS),
    )):
        raise RuntimeError(f"Actual memory/swap/PID limits are unsafe: {actual}")
    quota, period = actual["cpu.max"].split()
    if quota == "max" or int(quota) / int(period) != CPUS:
        raise RuntimeError(f"Actual CPU limit is unsafe: {actual['cpu.max']}")
    print(f"Actual enforced cgroup limits: {actual}")


def probe_image(image, version):
    name = "ha-resource-probe-" + uuid.uuid4().hex
    program = (
        "import pathlib, ha_satimage; "
        f"assert ha_satimage.__version__ == {version!r}, ha_satimage.__version__; "
        "p = pathlib.Path('/sys/fs/cgroup'); "
        f"assert (p/'memory.max').read_text().strip() == '{MEMORY}'; "
        "assert (p/'memory.swap.max').read_text().strip() == '0'; "
        f"assert (p/'pids.max').read_text().strip() == '{PIDS}'; "
        "q,t = (p/'cpu.max').read_text().split(); "
        f"assert q != 'max' and int(q)/int(t) == {CPUS}"
    )
    try:
        command(
            "docker", "run", "--name", name, "--rm", "--network", "none",
            "--memory", str(MEMORY), "--memory-swap", str(MEMORY),
            "--cpus", str(CPUS), "--pids-limit", str(PIDS),
            "--entrypoint", "/usr/local/bin/python3", image, "-I", "-c", program,
        )
    finally:
        # Clean up this probe only, including after a client timeout.
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
    print(f"Image version {version} and kernel enforcement verified without workload/mounts.")


def rollout(file, service, config, version):
    settings = config["services"][service]
    image = settings.get("image", "")
    if not image.startswith(("ghcr.io/bjoernhoefer/ha_satellite:",
                             "ghcr.io/bjoernhoefer/ha_satimage:")):
        raise RuntimeError("Expected a published ha_satellite/ha_satimage GHCR image tag.")
    compose(file, "pull", service)
    probe_image(image, version)
    compose(file, "stop", "--timeout", "10", service)
    try:
        compose(file, "up", "-d", "--no-deps", "--no-build", "--pull", "never",
                "--force-recreate", "--wait", "--wait-timeout", "180", service)
        container = compose(file, "ps", "-q", service).strip()
        if not container:
            raise RuntimeError("Compose did not return a running container")
        verify_container(container)
        print(command("docker", "stats", "--no-stream", container).strip())
    except (Exception, KeyboardInterrupt):
        # Never leave a workload running with unverified/incorrect limits.
        compose(file, "stop", "--timeout", "5", service)
        raise
    print("Rollout healthy. Monitor memory/events, PID peaks and UI responsiveness over scan cycles.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", required=True, type=Path,
                        help="absolute path to the exact Compose file used by host CD")
    parser.add_argument("--service", default="ha_satellite")
    parser.add_argument("--apply", action="store_true", help="back up and persist fixed resource limits")
    parser.add_argument("--rollout", action="store_true", help="pull, probe, recreate and verify the service")
    parser.add_argument("--yes", action="store_true", help="confirm changes non-interactively")
    parser.add_argument("--expected-version", default="1.7.2")
    args = parser.parse_args()
    file = args.compose_file
    if not file.is_absolute() or not file.is_file() or file.is_symlink():
        parser.error("--compose-file must be an existing absolute, non-symlink file")
    print(f"Selected {file}, service {args.service}; "
          f"apply={args.apply}, rollout={args.rollout}")
    print("Pause competing CD deployments first. No boot files, mounts, image tags or "
          "runner sudo permissions will be changed.")
    print("Target: 768 MiB RAM, zero extra swap, 1.5 CPUs, 128 PIDs/threads.")
    try:
        host_check()
        config = effective(file, args.service)
        check_compose_files(file, args.service)
        print("Current effective limits:", {
            key: config["services"][args.service].get(key, "unset") for key in LIMITS
        })
        if args.apply or args.rollout:
            print("Apply rewrites YAML formatting/comments; a private backup is kept. "
                  "Rollout interrupts this service and leaves it stopped on failed verification.")
            if not args.yes and input("Type APPLY to continue: ") != "APPLY":
                raise RuntimeError("Cancelled; no changes made.")
        if args.apply:
            apply_limits(file, args.service, config)
            config = effective(file, args.service)
        check_limits(config["services"][args.service])
        if args.rollout:
            rollout(file, args.service, config, args.expected_version)
        else:
            containers = compose(file, "ps", "-q", args.service).split()
            if containers:
                verify_container(containers[0])
            else:
                print("Service stopped. Runtime verification deferred until --rollout/CD.")
            print("Checks complete; no container started or restarted.")
    except yaml.YAMLError:
        print("STOP: Invalid Compose YAML; inspect locally (it may contain credentials).",
              file=sys.stderr)
        return 1
    except (RuntimeError, OSError, ValueError, KeyError, TypeError,
            subprocess.TimeoutExpired, EOFError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
