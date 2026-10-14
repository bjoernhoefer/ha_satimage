"""Host setup checks without contacting the Raspberry Pi or modifying its OS."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pi_setup", ROOT / "scripts/pi_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture
def host_compose(tmp_path):
    file = tmp_path / "docker-compose.yml"
    file.write_text(
        "name: satellite\nservices:\n  ha_satellite:\n"
        "    image: ghcr.io/bjoernhoefer/ha_satellite:latest\n"
        "    environment:\n      TEST_VALUE: '${TEST_VALUE:-unchanged}'\n"
        "    volumes: ['/data:/data']\n"
    )
    return file


def config(file, service):
    return yaml.safe_load(file.read_text())


def test_apply_backs_up_private_original_and_only_changes_limits(host_compose, monkeypatch):
    monkeypatch.setattr(setup, "effective", config)
    monkeypatch.setattr(setup, "check_compose_files", lambda *a: None)
    before = config(host_compose, "")
    original = host_compose.read_bytes()
    host_compose.chmod(0o640)
    setup.apply_limits(host_compose, "ha_satellite", before)
    after = config(host_compose, "")
    setup.check_limits(after["services"]["ha_satellite"])
    assert setup.without_limits(before, "ha_satellite") == setup.without_limits(after, "ha_satellite")
    backups = list(host_compose.parent.glob("*.backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert host_compose.stat().st_mode & 0o777 == 0o640
    setup.apply_limits(host_compose, "ha_satellite", after)
    assert list(host_compose.parent.glob("*.backup-*")) == backups
    assert not list(host_compose.parent.glob(".pi-setup-*"))


def test_apply_rejects_unrelated_effective_changes(host_compose, monkeypatch):
    before = config(host_compose, "")
    original = host_compose.read_bytes()

    def changed(file, service):
        result = config(file, service)
        result["services"][service]["image"] = "unexpected"
        return result

    monkeypatch.setattr(setup, "effective", changed)
    with pytest.raises(RuntimeError, match="unrelated"):
        setup.apply_limits(host_compose, "ha_satellite", before)
    assert host_compose.read_bytes() == original
    assert not list(host_compose.parent.glob("*.backup-*"))
    assert not list(host_compose.parent.glob(".pi-setup-*"))


def test_apply_updates_conflicting_deploy_limits(host_compose, monkeypatch):
    before = config(host_compose, "")
    before["services"]["ha_satellite"]["deploy"] = {
        "resources": {"limits": {"memory": "1g", "cpus": "4", "pids": 900}}
    }
    host_compose.write_text(yaml.safe_dump(before))
    monkeypatch.setattr(setup, "effective", config)
    setup.apply_limits(host_compose, "ha_satellite", before)
    updated = config(host_compose, "")["services"]["ha_satellite"]
    setup.check_limits(updated)
    assert updated["deploy"]["resources"]["limits"]["memory"] == str(setup.MEMORY)
    assert updated["deploy"]["resources"]["limits"]["pids"] == setup.PIDS


def test_check_default_does_not_change_files_or_start_container(host_compose, monkeypatch):
    original = host_compose.read_bytes()
    monkeypatch.setattr(sys, "argv", ["pi_setup.py", "--compose-file", str(host_compose)])
    monkeypatch.setattr(setup, "host_check", lambda: None)
    monkeypatch.setattr(setup, "effective", config)
    monkeypatch.setattr(setup, "check_compose_files", lambda *a: None)
    monkeypatch.setattr(setup, "apply_limits", lambda *a: pytest.fail("unexpected write"))
    monkeypatch.setattr(setup, "rollout", lambda *a: pytest.fail("unexpected rollout"))
    assert setup.main() == 1  # Missing limits are reported, not silently fixed.
    assert host_compose.read_bytes() == original


def test_no_mutation_when_host_lacks_memory_support(host_compose, monkeypatch):
    original = host_compose.read_bytes()
    monkeypatch.setattr(sys, "argv", [
        "pi_setup.py", "--compose-file", str(host_compose), "--apply", "--rollout", "--yes",
    ])
    monkeypatch.setattr(setup, "command", lambda *a: json.dumps({"MemoryLimit": False}))
    monkeypatch.setattr(setup, "effective", lambda *a: pytest.fail("should fail before Compose"))
    assert setup.main() == 1
    assert host_compose.read_bytes() == original


def test_rollout_probe_before_stop_and_verify_after_start(host_compose, monkeypatch):
    calls = []

    def compose(file, *args):
        calls.append(args[0])
        if args[0] == "up":
            assert "--no-deps" in args
        return "container-id" if args[0] == "ps" else ""

    monkeypatch.setattr(setup, "compose", compose)
    monkeypatch.setattr(setup, "probe_image", lambda *a: calls.append("probe"))
    monkeypatch.setattr(setup, "verify_container", lambda *a: calls.append("verify"))
    monkeypatch.setattr(setup, "command", lambda *a: "")
    setup.rollout(host_compose, "ha_satellite", config(host_compose, ""), "1.7.2")
    assert calls == ["pull", "probe", "stop", "up", "ps", "verify"]


@pytest.mark.parametrize("failure", ["probe", "verify"])
def test_rollout_fail_closed(host_compose, monkeypatch, failure):
    calls = []

    def compose(file, *args):
        calls.append(args[0])
        return "container-id"

    def fail(*args):
        raise RuntimeError("limit not enforced")

    monkeypatch.setattr(setup, "compose", compose)
    monkeypatch.setattr(setup, "probe_image", fail if failure == "probe" else lambda *a: None)
    monkeypatch.setattr(setup, "verify_container", fail)
    with pytest.raises(RuntimeError, match="not enforced"):
        setup.rollout(host_compose, "ha_satellite", config(host_compose, ""), "1.7.2")
    assert calls == (["pull"] if failure == "probe" else ["pull", "stop", "up", "ps", "stop"])


def test_actual_unlimited_cgroup_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "cgroup"
    root.mkdir()
    for name, value in {
        "memory.max": "max", "memory.swap.max": "max",
        "pids.max": "9585", "cpu.max": "150000 100000",
    }.items():
        (root / name).write_text(value)
    membership = tmp_path / "membership"
    membership.write_text("0::/\n")
    real_path = Path

    def path(value):
        return membership if str(value).startswith("/proc/") else root

    monkeypatch.setattr(setup, "Path", lambda value: (
        real_path(value) if value == "/" else path(value)
    ))
    monkeypatch.setattr(setup, "command", lambda *a: json.dumps([
        {"State": {"Running": True, "Pid": 42}},
    ]))
    with pytest.raises(RuntimeError, match="unsafe"):
        setup.verify_container("container")


def test_command_failure_does_not_print_secrets(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a, 1, "", "EUMETSAT_CONSUMER_SECRET=do-not-print",
    ))
    with pytest.raises(RuntimeError) as error:
        setup.command("docker", "compose")
    assert "do-not-print" not in str(error.value)


def test_existing_multifile_stack_is_rejected(host_compose, monkeypatch):
    monkeypatch.setattr(setup, "compose", lambda *a: "container")
    monkeypatch.setattr(setup, "command", lambda *a: json.dumps([{
        "Config": {"Labels": {"com.docker.compose.project.config_files":
                             f"{host_compose},/other/override.yml"}},
    }]))
    with pytest.raises(RuntimeError, match="different/multiple"):
        setup.check_compose_files(host_compose, "ha_satellite")
