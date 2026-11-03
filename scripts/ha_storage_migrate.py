#!/usr/bin/env python3
"""Move the ha_satimage data directory to another disk without data loss.

Host-side tool (Python 3 standard library + rsync, findmnt, docker). Default:
from the old ``ha_satellite`` location on the SD card to the NVMe disk of
the Raspberry Pi "nzbpi"::

    /home/bjoern/ha_satellite/data  ->  /mnt/data2/ha_satimage/data

Phases (each command can be repeated safely)::

    preflight   read-only checks: mount/UUID, free space, rights, containers
    copy        copy while the service keeps running (incremental, repeatable)
    finalize    service stopped: final sync, verification, marker in the source
    verify      read-only comparison of source and target
    status      show the recorded phase
    rollback    service stopped: copy data created at the target back to the
                source (nothing deleted) and remove the marker
    check-compose FILE   validate a Compose file for the new location
    apply-compose FILE   switch the service in a host Compose file to the new
                location (backup ``FILE.before-nvme``, idempotent; needs PyYAML)

Safety rules:

- Nothing is ever deleted in the source. The source gets a marker file
  ``.ha_satimage-migrated`` after a verified finalize; ha_satimage refuses
  to start on a directory with that marker, so an old container/Compose
  file can no longer write to it.
- The target is only written if it is empty or was created by this tool
  (state file ``<target parent>/.ha_satimage-migration.json``).
- The target's filesystem must have the expected UUID (``findmnt``) -
  if the disk is not mounted, nothing is created on the system disk.
- ``finalize`` and ``rollback`` refuse while any running container mounts
  the source or the target (or a path inside them), or while another
  ha_satellite/ha_satimage container mounts a directory above them (e.g.
  ``/mnt``). Other containers with only a broad parent mount (``/`` for a
  monitoring agent, ``/mnt/data2`` for a file browser) are reported by
  ``preflight`` but do not block. This tool never stops containers.
- After ``finalize`` a further ``copy``/``finalize`` is refused (it would
  overwrite data the new instance wrote); use ``rollback`` first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SOURCE = "/home/bjoern/ha_satellite/data"
DEFAULT_TARGET = "/mnt/data2/ha_satimage/data"
DEFAULT_UUID = "e16ccc22-790c-4f89-97f5-b7caca060bbd"
MARKER = ".ha_satimage-migrated"
STATE_NAME = ".ha_satimage-migration.json"
# Never copied: lock of the running instance, temporary files, the marker.
EXCLUDES = ("/" + MARKER, "/.instance.lock", "/tmp/", "*.part")
CRITICAL_PATTERNS = ("config.yaml", "*.json")
DEFAULT_RESERVE_MB = 2048


class MigrationError(Exception):
    pass


def run(args: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Runs a command (replaced in tests)."""
    result = subprocess.run(args, text=True, capture_output=capture)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise MigrationError(f"{' '.join(args[:3])}... failed ({result.returncode}): {detail}")
    return result


def log(message: str) -> None:
    print(message, flush=True)


# -- Checks ---------------------------------------------------------------------------
def existing_ancestor(path: Path) -> Path:
    current = Path(os.path.abspath(path))
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def fs_uuid_of(path: Path) -> str:
    """UUID of the filesystem containing ``path`` (or its nearest existing parent)."""
    result = run(["findmnt", "-no", "UUID,TARGET", "--target", str(existing_ancestor(path))])
    parts = result.stdout.split()
    return parts[0].lower() if parts and parts[0] != "" else ""


def directory_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


# Image/name markers of this service (old and new name). A parent mount of
# such a container (e.g. /mnt) can reach the data and blocks finalize; other
# containers with broad parent mounts (/, /mnt/data2) are only reported.
OWN_MARKERS = ("ha_satellite", "ha_satimage", "ha-satellite", "ha-satimage")


def mount_users(paths: list[Path]) -> dict[str, list[str]]:
    """Running containers by how they mount the paths.

    ``direct``: bind-mounts a path itself or something inside it.
    ``parent``: only bind-mounts a directory above it (e.g. ``/`` or ``/mnt``).
    """
    ids = run(["docker", "ps", "-q"]).stdout.split()
    if not ids:
        return {"direct": [], "parent": [], "own_parent": []}
    details = json.loads(run(["docker", "inspect", *ids]).stdout)
    wanted = [os.path.realpath(p) for p in paths]
    direct, parent, own_parent = set(), set(), set()
    for container in details:
        name = container.get("Name", "?").lstrip("/")
        image = str(container.get("Config", {}).get("Image", ""))
        own = any(marker in f"{name} {image}".lower() for marker in OWN_MARKERS)
        for mount in container.get("Mounts", []):
            source = os.path.realpath(mount.get("Source", ""))
            if any(_within(source, p) for p in wanted):
                direct.add(name)
            elif any(_within(p, source) for p in wanted):
                (own_parent if own else parent).add(name)
    parent -= direct | own_parent
    own_parent -= direct
    return {"direct": sorted(direct), "parent": sorted(parent), "own_parent": sorted(own_parent)}


def containers_using(paths: list[Path]) -> list[str]:
    """Running containers that can write the data: direct mounts, plus
    parent mounts of another ha_satellite/ha_satimage instance."""
    users = mount_users(paths)
    return sorted(set(users["direct"]) | set(users["own_parent"]))


def _within(a: str, b: str) -> bool:
    """True if path ``a`` is ``b`` or lies below it."""
    return a == b or a.startswith(b.rstrip("/") + "/")


def _related(a: str, b: str) -> bool:
    return _within(a, b) or _within(b, a)


class Migration:
    def __init__(self, source: Path, target: Path, uuid: str, reserve_mb: int = DEFAULT_RESERVE_MB,
                 acls_xattrs: bool = True) -> None:
        self.source = Path(os.path.abspath(source))
        self.target = Path(os.path.abspath(target))
        self.uuid = uuid.lower()
        self.reserve_mb = reserve_mb
        self.acls_xattrs = acls_xattrs
        if self.target == self.source or _related(str(self.source), str(self.target)):
            raise MigrationError("Source and target must not contain each other")

    # -- State ------------------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.target.parent / STATE_NAME

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise MigrationError(f"State file {self.state_path} unreadable: {exc}") from exc

    def _save_state(self, **changes) -> None:
        state = {**self.state(), **changes, "source": str(self.source), "target": str(self.target),
                 "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        partial = self.state_path.with_name(self.state_path.name + ".part")
        partial.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(partial, self.state_path)

    @property
    def phase(self) -> str:
        return self.state().get("phase", "new")

    # -- Checks -------------------------------------------------------------------
    def check_target_mount(self) -> None:
        actual = fs_uuid_of(self.target)
        if actual != self.uuid:
            raise MigrationError(
                f"{existing_ancestor(self.target)} is on filesystem UUID {actual or '?'}, expected "
                f"{self.uuid} - is the disk mounted? Nothing was created."
            )

    def check_source(self, allow_marker: bool = False) -> None:
        if not self.source.is_dir():
            raise MigrationError(f"Source {self.source} does not exist")
        if not (self.source / "config.yaml").exists():
            log(f"WARNING: {self.source}/config.yaml not found - is this the right directory?")
        if (self.source / MARKER).exists() and not allow_marker:
            raise MigrationError(f"Source {self.source} was already migrated ({MARKER} present)")

    def check_target_owned(self) -> None:
        """Target must be empty/missing or created by this tool for this source."""
        state = self.state()
        if state and state.get("source") != str(self.source):
            raise MigrationError(
                f"{self.state_path} belongs to a migration from {state.get('source')} - refusing"
            )
        if self.target.exists() and any(self.target.iterdir()) and not state:
            raise MigrationError(
                f"Target {self.target} is not empty and was not created by this tool - "
                "refusing to overwrite it. Move it away or choose another target."
            )

    def check_space(self) -> dict:
        needed = directory_size(self.source)
        present = directory_size(self.target) if self.target.exists() else 0
        free = shutil.disk_usage(existing_ancestor(self.target)).free
        required = max(needed - present, 0) * 1.1 + self.reserve_mb * 2**20
        info = {"source_mb": needed // 2**20, "target_mb": present // 2**20,
                "free_mb": free // 2**20, "required_mb": int(required // 2**20)}
        if free < required:
            raise MigrationError(
                f"Not enough free space at {self.target}: {info['free_mb']} MiB free, "
                f"{info['required_mb']} MiB required (incl. {self.reserve_mb} MiB reserve)"
            )
        return info

    def check_root(self) -> None:
        if os.geteuid() != 0:
            raise MigrationError("Run as root (sudo) so owners, permissions and ACLs are preserved")

    def check_stopped(self) -> None:
        users = containers_using([self.source, self.target])
        if users:
            raise MigrationError(
                "Containers still use the data: " + ", ".join(users)
                + " - stop them first (e.g. docker stop ha_satellite); this tool never stops services"
            )

    def preflight(self) -> dict:
        for tool in ("rsync", "findmnt"):
            if shutil.which(tool) is None:
                raise MigrationError(f"{tool} is not installed")
        self.check_source(allow_marker=self.phase == "finalized")
        self.check_target_mount()
        self.check_target_owned()
        info = self.check_space()
        info["phase"] = self.phase
        try:
            users = mount_users([self.source, self.target])
            info["containers"] = sorted(set(users["direct"]) | set(users["own_parent"]))
            if users["parent"]:
                # Broad mounts such as / or /mnt/data2 (monitoring, file browser);
                # they do not block finalize.
                info["parent_mounts"] = users["parent"]
        except (MigrationError, OSError, ValueError) as exc:
            info["containers"] = f"unknown ({exc})"
        if os.geteuid() != 0:
            info["warning"] = "not root: copy/finalize need sudo"
        return info

    # -- Copy ---------------------------------------------------------------------
    def _rsync(self, src: Path, dst: Path, delete: bool, dry_run: bool = False,
               checksum: bool = False) -> list[str]:
        args = ["rsync", "-aH", "--numeric-ids", "--itemize-changes"]
        if self.acls_xattrs:
            args += ["-AX"]
        if delete:
            args += ["--delete"]
        if dry_run:
            args += ["--dry-run"]
        if checksum:
            args += ["--checksum"]
        for pattern in EXCLUDES:
            args += ["--exclude", pattern]
        args += [f"{src}/", f"{dst}/"]
        result = run(args)
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _prepare_target(self) -> None:
        self.check_target_mount()
        self.check_target_owned()
        if not self.state():
            self.target.parent.mkdir(parents=True, exist_ok=True)
            self._save_state(phase="copying", started=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.target.mkdir(exist_ok=True)
        # Same owner and mode as the source directory itself.
        stat = self.source.stat()
        os.chown(self.target, stat.st_uid, stat.st_gid)
        os.chmod(self.target, stat.st_mode & 0o7777)

    def copy(self) -> None:
        if self.phase == "finalized":
            raise MigrationError("Already finalized - a copy would overwrite newer data at the target. "
                                 "Use rollback first.")
        self.check_root()
        self.check_source()
        self.check_space()
        self._prepare_target()
        log(f"Copying {self.source} -> {self.target} (service may keep running) ...")
        # No --delete while the service runs; finalize removes stale files.
        changes = self._rsync(self.source, self.target, delete=False)
        self._save_state(phase="copying", last_copy=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        log(f"Copy done ({len(changes)} changed entries). Repeat any time; then stop the service and run finalize.")

    def verify(self, full_checksum: bool = False) -> None:
        differences = self._rsync(self.source, self.target, delete=True, dry_run=True,
                                  checksum=full_checksum)
        # rsync lists the root directory's own attributes as "." - only content counts.
        differences = [d for d in differences if not d.endswith(" ./")]
        if differences:
            raise MigrationError(
                f"Target differs from the source in {len(differences)} entries, e.g.: "
                + "; ".join(differences[:5])
            )
        checked = 0
        for pattern in CRITICAL_PATTERNS:
            for path in self.source.rglob(pattern):
                if not path.is_file() or path.name == MARKER:
                    continue
                relative = path.relative_to(self.source)
                if any(part == "tmp" for part in relative.parts[:1]):
                    continue
                other = self.target / relative
                if not other.is_file() or _sha256(path) != _sha256(other):
                    raise MigrationError(f"Checksum mismatch: {relative}")
                checked += 1
        log(f"Verified: no differences, {checked} configuration/index files checksum-identical"
            + (" (full checksum)" if full_checksum else ""))

    def finalize(self, full_checksum: bool = False) -> None:
        if self.phase == "finalized":
            if not (self.source / MARKER).exists():
                self._write_marker()
                log("Marker restored.")
            raise MigrationError("Already finalized - nothing to do (rollback first to repeat)")
        if self.phase == "new":
            raise MigrationError("Run copy first")
        self.check_root()
        self.check_source(allow_marker=True)
        self.check_stopped()
        self._prepare_target()
        log("Final sync (service stopped) ...")
        self._rsync(self.source, self.target, delete=True)
        self.verify(full_checksum)
        self._write_marker()
        self._save_state(phase="finalized", finalized=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        log(f"Finalized. Switch the deployment to {self.target}, then start the service. "
            f"{self.source} is kept unchanged (plus {MARKER}).")

    def _write_marker(self) -> None:
        marker = self.source / MARKER
        marker.write_text(
            json.dumps({"migrated_to": str(self.target),
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n",
            encoding="utf-8",
        )

    def rollback(self) -> None:
        if self.phase != "finalized":
            raise MigrationError("Nothing to roll back (not finalized)")
        self.check_root()
        self.check_stopped()
        log(f"Copying data written at {self.target} back to {self.source} (nothing deleted) ...")
        self._rsync(self.target, self.source, delete=False)
        (self.source / MARKER).unlink(missing_ok=True)
        self._save_state(phase="rolled_back", rolled_back=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        log(f"Rolled back. Point the deployment at {self.source} again. {self.target} is kept.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# -- Compose check --------------------------------------------------------------------
def check_compose_config(config: dict, target: str, uuid: str, service: str | None = None) -> list[str]:
    """Problems of an effective Compose configuration (``docker compose config``)."""
    services = config.get("services", {})
    if service is None:
        candidates = [name for name, svc in services.items()
                      if any(_mount_target(v) == "/data" for v in svc.get("volumes", []))]
        if len(candidates) != 1:
            return [f"Expected exactly one service with /data, found {candidates or 'none'}"]
        service = candidates[0]
    svc = services.get(service)
    if svc is None:
        return [f"Service {service} not found"]
    problems = []
    data = [v for v in svc.get("volumes", []) if _mount_target(v) == "/data"]
    if not data or not isinstance(data[0], dict):
        problems.append("/data is not a long-syntax bind mount")
    else:
        volume = data[0]
        if os.path.abspath(volume.get("source", "")) != os.path.abspath(target):
            problems.append(f"/data comes from {volume.get('source')}, expected {target}")
        if (volume.get("bind") or {}).get("create_host_path", True) is not False:
            problems.append("/data: bind.create_host_path must be false")
    if not any(_mount_target(v) == "/run/host-disk-by-uuid" for v in svc.get("volumes", [])):
        problems.append("/dev/disk/by-uuid is not mounted at /run/host-disk-by-uuid (identity check)")
    environment = svc.get("environment") or {}
    if isinstance(environment, list):
        environment = dict(item.split("=", 1) for item in environment if "=" in item)
    uuids = [u.strip().lower() for u in str(environment.get("HA_SATIMAGE_REQUIRE_FS_UUID", "")).split(",")]
    if uuid.lower() not in uuids:
        problems.append(f"HA_SATIMAGE_REQUIRE_FS_UUID does not contain {uuid}")
    logging_options = (svc.get("logging") or {}).get("options") or {}
    if (svc.get("logging") or {}).get("driver", "json-file") == "json-file" and "max-size" not in logging_options:
        problems.append("Docker log is not size-limited (logging.options.max-size)")
    for volume in svc.get("volumes", []):
        propagation = ((volume.get("bind") or {}).get("propagation") if isinstance(volume, dict) else None)
        if propagation in ("rslave", "slave", "rshared", "shared") and _mount_target(volume) == "/mnt":
            problems.append("/mnt with propagation %s exposes unmounted mount points" % propagation)
    return problems


LOG_OPTIONS = {"max-size": "2m", "max-file": "2"}
BY_UUID_BIND = {
    "type": "bind", "source": "/dev/disk/by-uuid", "target": "/run/host-disk-by-uuid",
    "read_only": True, "bind": {"create_host_path": False},
}


def _long_bind(volume, source: str | None = None) -> dict:
    """Long-syntax bind of a volume entry with ``create_host_path: false``."""
    if isinstance(volume, dict):
        result = dict(volume)
    else:
        parts = str(volume).split(":")
        result = {"type": "bind", "source": parts[0], "target": parts[1]}
        if len(parts) > 2 and "ro" in parts[2].split(","):
            result["read_only"] = True
    if source is not None:
        result["source"] = source
    result["type"] = "bind"
    result["bind"] = {**(result.get("bind") or {}), "create_host_path": False}
    return result


def compose_for_target(document: dict, target: str, uuid: str, service: str | None = None,
                       frames_uuid: str | None = None) -> tuple[dict, str]:
    """Returns the Compose document with the service switched to ``target``
    (and the service name). Only the /data source, bind options, the
    identity bind, the UUID variable and Docker's log limit are changed."""
    import copy

    services = (document or {}).get("services") or {}
    if service is None:
        candidates = [name for name, svc in services.items()
                      if any(_mount_target(v) == "/data" for v in (svc or {}).get("volumes", []))]
        if len(candidates) != 1:
            raise MigrationError(
                f"Expected exactly one service with /data in this file, found {candidates or 'none'}"
                " - pass --service or edit the file manually")
        service = candidates[0]
    if service not in services:
        raise MigrationError(f"Service {service} is not defined in this file - edit its file manually")
    result = copy.deepcopy(document)
    svc = result["services"][service]
    volumes = []
    for volume in svc.get("volumes") or []:
        mount = _mount_target(volume)
        if mount == "/data":
            volumes.append(_long_bind(volume, source=target))
        elif mount == "/run/host-disk-by-uuid":
            continue
        elif not isinstance(volume, dict) or volume.get("type", "bind") == "bind":
            source = volume.get("source") if isinstance(volume, dict) else str(volume).split(":")[0]
            # Named volumes (no path) stay as they are.
            volumes.append(_long_bind(volume) if str(source).startswith(("/", ".", "$")) else volume)
        else:
            volumes.append(volume)
    if not any(_mount_target(v) == "/data" for v in volumes):
        raise MigrationError(f"Service {service} has no /data volume")
    volumes.append(dict(BY_UUID_BIND))
    svc["volumes"] = volumes
    wanted = {"HA_SATIMAGE_REQUIRE_FS_UUID": uuid}
    if frames_uuid:
        wanted["HA_SATIMAGE_FRAMES_FS_UUID"] = frames_uuid
    environment = svc.get("environment")
    if isinstance(environment, list):
        environment = [item for item in environment
                       if str(item).split("=", 1)[0] not in wanted]
        environment.extend(f"{key}={value}" for key, value in wanted.items())
    else:
        environment = {**dict(environment or {}), **wanted}
    svc["environment"] = environment
    logging_config = svc.get("logging") or {}
    if logging_config.get("driver", "json-file") == "json-file":
        options = dict(logging_config.get("options") or {})
        options.setdefault("max-size", LOG_OPTIONS["max-size"])
        options.setdefault("max-file", LOG_OPTIONS["max-file"])
        svc["logging"] = {**logging_config, "driver": "json-file", "options": options}
    return result, service


def apply_compose(path: Path, target: str, uuid: str, service: str | None = None,
                  frames_uuid: str | None = None) -> bool:
    """Rewrites a host Compose file for the new location. Keeps the first
    original as ``<file>.before-nvme``. Returns whether the file changed."""
    try:
        import yaml
    except ImportError as exc:
        raise MigrationError("PyYAML is required: sudo apt install python3-yaml") from exc
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise MigrationError(f"{path} must be an existing absolute, non-symlink file")
    original_text = path.read_text()
    document = yaml.safe_load(original_text)
    updated, service = compose_for_target(document, target, uuid, service, frames_uuid)
    if updated == document:
        log(f"{path}: service {service} already uses {target} - unchanged")
        return False
    backup = path.with_name(path.name + ".before-nvme")
    if not backup.exists():
        shutil.copy2(path, backup)
        log(f"Backup of the Compose file: {backup}")
    stat = path.stat()
    temporary = path.with_name(f".{path.name}.part")
    temporary.write_text(yaml.safe_dump(updated, sort_keys=False, default_flow_style=False))
    os.chmod(temporary, stat.st_mode & 0o7777)
    try:
        os.chown(temporary, stat.st_uid, stat.st_gid)
    except PermissionError:
        pass
    os.replace(temporary, path)
    log(f"{path}: service {service} switched to {target} (UUID {uuid}); "
        "comments/formatting of the file are not kept (see the backup)")
    return True


def _mount_target(volume) -> str | None:
    if isinstance(volume, dict):
        return volume.get("target")
    parts = str(volume).split(":")
    return parts[1] if len(parts) > 1 else None


# -- CLI ------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--expect-uuid", default=DEFAULT_UUID)
    parser.add_argument("--reserve-mb", type=int, default=DEFAULT_RESERVE_MB)
    parser.add_argument("--no-acls-xattrs", action="store_true",
                        help="do not copy ACLs/xattrs (target filesystem without support)")
    parser.add_argument("--full-checksum", action="store_true",
                        help="verify every file by content (reads all data once more)")
    parser.add_argument("--frames-uuid", default=None,
                        help="apply-compose: UUID of a frame location outside /data on another disk")
    parser.add_argument("--service", default=None,
                        help="Compose service (default: the one that mounts /data)")
    parser.add_argument("command", choices=["preflight", "copy", "finalize", "verify", "status",
                                            "rollback", "check-compose", "apply-compose"])
    parser.add_argument("compose_file", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.command == "apply-compose":
            if not args.compose_file:
                raise MigrationError("apply-compose needs the Compose file")
            apply_compose(Path(args.compose_file), args.target, args.expect_uuid, args.service,
                          args.frames_uuid)
            return 0
        if args.command == "check-compose":
            if not args.compose_file:
                raise MigrationError("check-compose needs the Compose file")
            result = run(["docker", "compose", "-f", args.compose_file, "config", "--format", "json"])
            problems = check_compose_config(json.loads(result.stdout), args.target, args.expect_uuid,
                                            args.service)
            for problem in problems:
                log(f"PROBLEM: {problem}")
            log("Compose ok" if not problems else f"{len(problems)} problem(s)")
            return 0 if not problems else 1
        migration = Migration(Path(args.source), Path(args.target), args.expect_uuid,
                              args.reserve_mb, not args.no_acls_xattrs)
        if args.command == "preflight":
            log(json.dumps(migration.preflight(), indent=2))
        elif args.command == "status":
            log(json.dumps({"phase": migration.phase, **migration.state(),
                            "source_marker": (migration.source / MARKER).exists()}, indent=2))
        elif args.command == "copy":
            migration.copy()
        elif args.command == "verify":
            migration.verify(args.full_checksum)
        elif args.command == "finalize":
            migration.finalize(args.full_checksum)
        elif args.command == "rollback":
            migration.rollback()
    except MigrationError as exc:
        log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
