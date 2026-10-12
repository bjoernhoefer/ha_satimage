"""Check the deployment contract without accessing a deployment host."""

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


def test_deployment_contract(deploy_job):
    workflow = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
    build = workflow["jobs"]["build"]
    assert workflow["env"]["IMAGE_NAME"] == "${{ github.repository_owner }}/ha_satimage"
    assert build["outputs"]["digest"] == "${{ steps.build.outputs.digest }}"
    assert any(
        step.get("id") == "build" and step["uses"] == "docker/build-push-action@v6"
        for step in build["steps"]
    )
    assert deploy_job["needs"] == "build"
    assert deploy_job["if"] == (
        "github.ref == 'refs/heads/main' && "
        "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
    )
    assert deploy_job["runs-on"] == ["self-hosted", "linux", "ARM64", "deploy-satellite"]
    assert deploy_job["permissions"] == {}
    assert deploy_job["concurrency"] == {
        "group": "deploy-ha_satellite", "cancel-in-progress": False,
    }
    assert deploy_job["timeout-minutes"] == 20
    assert all("uses" not in step for step in deploy_job["steps"])
    guard, deploy = deploy_job["steps"]
    assert guard["id"] == "superseded"
    assert guard["env"]["REPOSITORY"] == "${{ github.repository }}"
    assert deploy["if"] == "steps.superseded.outputs.skip == 'false'"
    assert deploy["env"]["DIGEST"] == "${{ needs.build.outputs.digest }}"
    assert deploy["env"]["IMAGE"] == "${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}"
    assert all("${{" not in step["run"] for step in deploy_job["steps"])


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
        'touch "$GITHUB_OUTPUT"\n', DIGEST=digest,
    )
    assert result.returncode != 0
    assert "::error::Invalid image digest" in result.stdout
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("exit_code", [0, 1])
def test_digest_deployment_and_failure_propagation(tmp_path, deploy_job, exit_code):
    result = run_step(
        tmp_path, deploy_job["steps"][1], "sudo",
        'if [[ "$5" != "ha_satellite" ]]; then\n'
        '  echo "ha-deploy: unknown stack \'$5\'" >&2\n'
        '  exit 2\n'
        'fi\n'
        'printf "%s\\n" "$@" > "$GITHUB_OUTPUT"\nexit "$DEPLOY_EXIT"\n',
        IMAGE="ghcr.io/bjoernhoefer/ha_satimage", DIGEST=DIGEST,
        DEPLOY_EXIT=str(exit_code),
    )
    assert result.returncode == exit_code
    assert (tmp_path / "output").read_text().splitlines() == [
        "-n", "-u", "bjoern", "/usr/local/bin/ha-deploy", "ha_satellite",
        f"ghcr.io/bjoernhoefer/ha_satimage@{DIGEST}", SHA,
    ]


def test_compose_disables_watchtower():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert "watchtower" not in compose["services"]
    assert "com.centurylinklabs.watchtower.enable=false" in (
        compose["services"]["ha_satimage"]["labels"]
    )
