"""Check the deployment contract without accessing a deployment host."""

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def deploy_job():
    workflow = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
    return workflow["jobs"]["deploy"]


@pytest.fixture
def targets_job():
    workflow = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
    return workflow["jobs"]["targets"]


PI = {
    "name": "pi",
    "runs_on": ["self-hosted", "linux", "ARM64", "deploy-satellite"],
    "deploy_user": "bjoern",
    "concurrency": "deploy-ha_satellite",
}
AZURE = {
    "name": "azure",
    "runs_on": ["self-hosted", "linux", "X64", "deploy-satellite-azure"],
    "deploy_user": "ha-satimage",
    "concurrency": "deploy-ha_satellite-azure",
}


def test_deployment_contract(deploy_job, targets_job):
    workflow = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
    build = workflow["jobs"]["build"]
    assert workflow["env"]["IMAGE_NAME"] == "${{ github.repository_owner }}/ha_satimage"
    assert workflow["env"]["DEPLOY_IMAGE_NAME"] == "${{ github.repository_owner }}/ha_satellite"
    assert build["outputs"]["digest"] == "${{ steps.build.outputs.digest }}"
    assert any(
        step.get("id") == "build" and step["uses"] == "docker/build-push-action@v6"
        for step in build["steps"]
    )
    dispatch = workflow[True]["workflow_dispatch"]["inputs"]["deploy_targets"]
    assert dispatch["options"] == ["configured", "pi", "azure", "both", "none"]
    assert dispatch["default"] == "configured"
    assert targets_job["if"] == (
        "github.ref == 'refs/heads/main' && "
        "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
    )
    assert targets_job["runs-on"] == "ubuntu-latest"
    assert targets_job["permissions"] == {}
    resolve = targets_job["steps"][0]
    assert resolve["env"] == {
        "REQUESTED": "${{ inputs.deploy_targets }}",
        "CONFIGURED": "${{ vars.DEPLOY_TARGETS }}",
    }
    assert "${{" not in resolve["run"]
    assert deploy_job["needs"] == ["build", "targets"]
    assert deploy_job["if"] == "needs.targets.outputs.matrix != '[]'"
    assert deploy_job["runs-on"] == "${{ matrix.target.runs_on }}"
    assert deploy_job["strategy"] == {
        "fail-fast": False,
        "matrix": {"target": "${{ fromJSON(needs.targets.outputs.matrix) }}"},
    }
    assert deploy_job["permissions"] == {}
    assert deploy_job["concurrency"] == {
        "group": "${{ matrix.target.concurrency }}", "cancel-in-progress": False,
    }
    assert deploy_job["timeout-minutes"] == 20
    assert all("uses" not in step for step in deploy_job["steps"])
    guard, deploy = deploy_job["steps"]
    assert guard["id"] == "superseded"
    assert guard["env"]["REPOSITORY"] == "${{ github.repository }}"
    assert deploy["if"] == "steps.superseded.outputs.skip == 'false'"
    assert deploy["env"]["DIGEST"] == "${{ needs.build.outputs.digest }}"
    assert deploy["env"]["IMAGE"] == "${{ env.REGISTRY }}/${{ env.DEPLOY_IMAGE_NAME }}"
    assert deploy["env"]["DEPLOY_USER"] == "${{ matrix.target.deploy_user }}"
    assert all("${{" not in step["run"] for step in deploy_job["steps"])


def test_build_publishes_both_image_names_with_the_same_digest():
    workflow = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
    build = workflow["jobs"]["build"]
    metadata = next(step for step in build["steps"] if step.get("id") == "meta")
    assert metadata["with"]["images"].splitlines() == [
        "${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}",
        "${{ env.REGISTRY }}/${{ env.DEPLOY_IMAGE_NAME }}",
    ]
    publish = next(step for step in build["steps"] if step.get("id") == "build")
    assert publish["with"]["tags"] == "${{ steps.meta.outputs.tags }}"
    assert publish["with"]["push"] == "${{ github.event_name != 'pull_request' }}"
    assert build["outputs"]["digest"] == "${{ steps.build.outputs.digest }}"


def run_step(tmp_path, step, command, script, **env):
    executable = tmp_path / command
    executable.write_text("#!/bin/bash\n" + script)
    executable.chmod(0o755)
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GITHUB_SHA": SHA,
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "REPOSITORY": "bjoernhoefer/ha_satimage",
            **env,
        },
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "requested,configured,expected",
    [
        ("", "", [PI]),
        ("configured", "", [PI]),
        ("configured", "azure", [AZURE]),
        ("configured", "pi,azure", [PI, AZURE]),
        ("configured", " Azure , PI ", [PI, AZURE]),
        ("configured", "both", [PI, AZURE]),
        ("configured", "none", []),
        ("pi", "azure", [PI]),
        ("azure", "", [AZURE]),
        ("both", "none", [PI, AZURE]),
        ("none", "both", []),
    ],
)
def test_rollout_target_resolution(tmp_path, targets_job, requested, configured, expected):
    result = run_step(
        tmp_path, targets_job["steps"][0], "unused", "",
        REQUESTED=requested, CONFIGURED=configured, GITHUB_STEP_SUMMARY="",
    )
    assert result.returncode == 0, result.stderr
    output = (tmp_path / "output").read_text()
    assert output.startswith("matrix=") and output.endswith("\n")
    assert json.loads(output.removeprefix("matrix=")) == expected
    names = " ".join(target["name"] for target in expected) or "none"
    assert f"Rollout targets: {names}" in result.stdout


@pytest.mark.parametrize(
    "configured",
    ["nas", "pi;rm -rf /", "pi,*", "$(id)", "pi\nazure=x", "azure,pi4"],
)
def test_invalid_rollout_targets_fail(tmp_path, targets_job, configured):
    result = run_step(
        tmp_path, targets_job["steps"][0], "unused", "",
        REQUESTED="configured", CONFIGURED=configured, GITHUB_STEP_SUMMARY="",
    )
    assert result.returncode != 0
    assert "::error::" in result.stdout
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("main_sha,skip", [(SHA, "false"), ("c" * 40, "true")])
def test_superseded_build(tmp_path, deploy_job, main_sha, skip):
    result = run_step(
        tmp_path, deploy_job["steps"][0], "git",
        'printf "%s\\n" "$@" > "$GITHUB_OUTPUT.args"\n'
        'printf "%s\\trefs/heads/main\\n" "$MAIN_SHA"\n',
        MAIN_SHA=main_sha,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "output").read_text() == f"skip={skip}\n"
    assert (tmp_path / "output.args").read_text().splitlines() == [
        "ls-remote", "--exit-code",
        "https://github.com/bjoernhoefer/ha_satimage", "refs/heads/main",
    ]
    assert ("::notice::" in result.stdout) == (skip == "true")


@pytest.mark.parametrize("script", ["exit 128\n", "exit 0\n"])
def test_main_lookup_failure_does_not_deploy(tmp_path, deploy_job, script):
    result = run_step(tmp_path, deploy_job["steps"][0], "git", script)
    assert result.returncode != 0
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "digest",
    ["", "latest", "sha256:" + "b" * 63, "sha256:" + "b" * 65,
     "sha256:" + "B" * 64, DIGEST + "\necho injected"],
)
def test_invalid_digest_never_calls_sudo(tmp_path, deploy_job, digest):
    result = run_step(
        tmp_path, deploy_job["steps"][1], "sudo",
        'touch "$GITHUB_OUTPUT"\n', DIGEST=digest, DEPLOY_USER="bjoern",
    )
    assert result.returncode != 0
    assert "::error::Invalid image digest" in result.stdout
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("user", ["", "root;id", "-n", "Bjoern", "a b"])
def test_invalid_deploy_user_never_calls_sudo(tmp_path, deploy_job, user):
    result = run_step(
        tmp_path, deploy_job["steps"][1], "sudo",
        'touch "$GITHUB_OUTPUT"\n', DIGEST=DIGEST, DEPLOY_USER=user,
    )
    assert result.returncode != 0
    assert "::error::Invalid deploy user" in result.stdout
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("target", [PI, AZURE], ids=["pi", "azure"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_digest_deployment_and_failure_propagation(tmp_path, deploy_job, exit_code, target):
    result = run_step(
        tmp_path, deploy_job["steps"][1], "sudo",
        'if [[ "$5" != "ha_satellite" ]]; then\n'
        '  echo "ha-deploy: unknown stack \'$5\'" >&2\n'
        '  exit 2\n'
        'fi\n'
        'if [[ "$6" != "ghcr.io/bjoernhoefer/ha_satellite@${DIGEST}" ]]; then\n'
        '  echo "ha-deploy: image must be ghcr.io/bjoernhoefer/ha_satellite@sha256:<digest>" >&2\n'
        '  exit 2\n'
        'fi\n'
        'printf "%s\\n" "$@" > "$GITHUB_OUTPUT"\nexit "$DEPLOY_EXIT"\n',
        IMAGE="ghcr.io/bjoernhoefer/ha_satellite", DIGEST=DIGEST,
        DEPLOY_EXIT=str(exit_code), DEPLOY_USER=target["deploy_user"],
    )
    assert result.returncode == exit_code
    assert (tmp_path / "output").read_text().splitlines() == [
        "-n", "-u", target["deploy_user"], "/usr/local/bin/ha-deploy", "ha_satellite",
        f"ghcr.io/bjoernhoefer/ha_satellite@{DIGEST}", SHA,
    ]


def test_compose_disables_watchtower():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert "watchtower" not in compose["services"]
    assert "com.centurylinklabs.watchtower.enable=false" in (
        compose["services"]["ha_satimage"]["labels"]
    )


def test_container_resource_limits():
    service = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["ha_satimage"]
    assert service["mem_limit"] == "${HA_SATIMAGE_MEM_LIMIT:-768m}"
    assert service["memswap_limit"] == service["mem_limit"]
    assert service["cpus"] == "${HA_SATIMAGE_CPUS:-1.5}"
    assert service["pids_limit"] == "${HA_SATIMAGE_PIDS_LIMIT:-128}"
    dockerfile = (ROOT / "docker/Dockerfile").read_text()
    for variable in (
        "OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS", "NUMEXPR_MAX_THREADS", "VECLIB_MAXIMUM_THREADS",
        "BLIS_NUM_THREADS",
    ):
        assert f"{variable}=1" in dockerfile


def _interpolate(value, env=None):
    """Minimal Compose ``${VAR:-default}`` interpolation (no docker needed)."""
    import re

    env = env or {}
    if isinstance(value, dict):
        return {k: _interpolate(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, env) for v in value]
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: env.get(m[1]) or m[2], value)
    return value


def _environment(service) -> dict:
    return dict(item.split("=", 1) for item in service["environment"])


def test_compose_stores_data_on_the_checked_nvme_without_sd_fallback():
    import importlib.util

    spec = importlib.util.spec_from_file_location("migrate", ROOT / "scripts/ha_storage_migrate.py")
    migrate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migrate)
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["ha_satimage"]
    effective = {"services": {"ha_satimage": {**_interpolate(service), "environment": _interpolate(_environment(service))}}}
    assert migrate.check_compose_config(
        effective, migrate.DEFAULT_TARGET, migrate.DEFAULT_UUID, "ha_satimage"
    ) == []
    # Every bind refuses to create a missing host path (unmounted disk).
    for volume in service["volumes"]:
        assert volume["type"] == "bind" and volume["bind"]["create_host_path"] is False
        assert "propagation" not in volume["bind"]
    # Host path stays configurable.
    custom = _interpolate(service, {"HOST_DATA_DIR": "/srv/ha"})
    assert custom["volumes"][0]["source"] == "/srv/ha"
    # Docker's own log (system disk) is bounded.
    assert service["logging"]["options"]["max-size"] == "2m"
    assert int(service["logging"]["options"]["max-file"]) <= 3


def test_image_keeps_runtime_writes_off_the_container_layer():
    dockerfile = (ROOT / "docker/Dockerfile").read_text()
    assert "ENV TMPDIR=/data/tmp" in dockerfile
    # /data does not exist at build time: TMPDIR must be set after every RUN.
    lines = dockerfile.splitlines()
    tmpdir_line = next(i for i, line in enumerate(lines) if "TMPDIR=/data/tmp" in line)
    assert not any(line.startswith("RUN ") for line in lines[tmpdir_line:])
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile and "compileall" in dockerfile
    assert "HA_SATIMAGE_STDOUT_LOG_LEVEL=WARNING" in dockerfile
    assert "VOLUME" not in "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--no-access-log" in dockerfile
    assert "--interval=2m" in dockerfile


def test_entrypoint_checks_storage_before_writing():
    script = (ROOT / "docker/entrypoint.sh").read_text()
    subprocess.run(["bash", "-n", str(ROOT / "docker/entrypoint.sh")], check=True)
    guard = script.index("storage_guard --no-write")
    for write in ("chown -R", "install -d", "find \"$TMPDIR\""):
        assert script.index(write) > guard, write
    # Temp cleanup only in the dedicated directory.
    assert '"${TMPDIR:-}" = "$DATA_DIR/tmp"' in script
    assert script.count("|| exit 78") == 3


def test_env_example_documents_storage_variables():
    example = (ROOT / ".env.example").read_text()
    for name in ("HOST_DATA_DIR=/mnt/data2/ha_satimage/data", "HA_SATIMAGE_REQUIRE_FS_UUID",
                 "HA_SATIMAGE_FRAMES_FS_UUID", "HA_SATIMAGE_MIN_FREE_MB",
                 "HA_SATIMAGE_RENDER_MEMORY_MB"):
        assert name in example


def test_deployment_names_are_not_renamed():
    """The host stack keeps its ha_satellite name: a rename would start a
    second container next to the old one on the same data."""
    workflow = (ROOT / ".github/workflows/build.yml").read_text()
    assert '/usr/local/bin/ha-deploy ha_satellite "${IMAGE}@${DIGEST}"' in workflow
    assert '"concurrency":"deploy-ha_satellite"' in workflow
    setup = (ROOT / "scripts/pi_setup.py").read_text()
    assert 'parser.add_argument("--service", default="ha_satellite")' in setup


def test_host_helpers_are_valid():
    subprocess.run(["bash", "-n", str(ROOT / "scripts/ha_measure.sh")], check=True)
    subprocess.run(["bash", "-n", str(ROOT / "scripts/ha_nzbpi_switch.sh")], check=True)
    unit = (ROOT / "docker/systemd/ha-satimage-after-mount.service").read_text()
    assert "RequiresMountsFor=/mnt/data2/ha_satimage/data" in unit
    assert "docker start ha_satellite" in unit


def test_switch_script_order_and_safety():
    script = (ROOT / "scripts/ha_nzbpi_switch.sh").read_text()
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    # Dry check unless --run; never deletes or rolls back by itself.
    assert '[ "${1:-}" = "--run" ] && RUN=1' in code
    assert "rm -" not in code and "--delete" not in code
    # Rollback commands are only printed for the operator, never executed.
    assert '"${MIGRATE[@]}" rollback' not in code
    # The Compose file is switched and validated before the service stops;
    # systemd only after the verified start.
    main = code[code.index('step "1. Checks"'):]
    order = [main.index(marker) for marker in (
        "preflight", " copy", "apply-compose", "check-compose", "docker stop",
        " finalize", "docker compose -p", "/api/diagnostics", "systemctl enable",
    )]
    assert order == sorted(order)
    assert "findmnt -no UUID" in code and "EXPECT_UUID:-e16ccc22-790c-4f89-97f5-b7caca060bbd" in code
    # Same Compose project and service: no second instance next to the old one.
    assert 'docker compose -p "$PROJECT" -f "$FILES"' in code
    assert "CONTAINER=${CONTAINER:-ha_satellite}" in code
