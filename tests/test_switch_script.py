"""End-to-end run of scripts/ha_nzbpi_switch.sh against stubbed host tools.

docker, findmnt, curl and systemctl are replaced by small stubs; the
migration itself (rsync, state file, marker, Compose rewrite) is real and
works on temporary directories. Needs root (the script refuses otherwise),
rsync and PyYAML.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
UUID = "e16ccc22-790c-4f89-97f5-b7caca060bbd"
pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or shutil.which("rsync") is None or shutil.which("bash") is None,
    reason="needs root, rsync and bash",
)

STUBS = {
    "findmnt": r"""#!/bin/bash
target="${@: -1}"
uuid="$STUB_UUID"
[ "$target" = / ] && uuid=root-uuid
case "$*" in *UUID,TARGET*) echo "$uuid $target";; *) echo "$uuid";; esac
""",
    "docker": r"""#!/bin/bash
echo "docker $*" >> "$STUB_LOG"
state="$STUB_DIR/mount"
case "$1" in
  compose)
    shift
    [ "$1" = version ] && exit 0
    file=""; while [ $# -gt 0 ]; do
      case "$1" in -f) file="$2"; shift;; config) "$STUB_PY" -c 'import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))' "$file"; exit 0;;
                  up) grep -q "source: $STUB_TARGET" "$file" && echo "$STUB_TARGET" > "$state"; [ -n "$STUB_FAIL_UP" ] && exit 1; exit 0;; esac
      shift; done;;
  inspect)
    if [ "$2" != -f ] && [ -n "$STUB_PS_JSON" ]; then echo "$STUB_PS_JSON"; exit 0; fi
    if [ "$2" = -f ]; then
      case "$3" in
        *project.working_dir*) dirname "$STUB_COMPOSE";;
        *project.config_files*) echo "$STUB_COMPOSE";;
        *compose.project\"*) echo ha_satellite;;
        *compose.service*) echo ha_satellite;;
        *Mounts*) cat "$state";;
        *Health*) echo healthy;;
      esac
    fi;;
  ps) [ -n "$STUB_PS_JSON" ] && echo other; exit 0;;
  port) echo "0.0.0.0:6060";;
  stop) ;;
esac
""",
    "curl": r"""#!/bin/bash
case "$*" in
  *healthz*) echo ok;;
  *diagnostics*) echo "{\"storage\": {\"paused\": false, \"problem\": null, \"paths\": [{\"path\": \"/data\", \"fs_uuid\": \"$STUB_UUID\", \"free_mb\": 900000}]}}";;
esac
""",
    "systemctl": r"""#!/bin/bash
echo "systemctl $*" >> "$STUB_LOG"
[ "$1" = show ] && echo success
exit 0
""",
    "systemd-analyze": "#!/bin/bash\nexit 0\n",
    "ionice": '#!/bin/bash\nshift\nexec "$@"\n',
}


@pytest.fixture
def host(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (bin_dir / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (bin_dir / "python3").chmod(0o755)
    source = tmp_path / "home/bjoern/ha_satellite/data"
    (source / "frames/wien").mkdir(parents=True)
    (source / "config.yaml").write_text("regions: []\n")
    (source / "frames/wien/a.png").write_bytes(b"png" * 50)
    (tmp_path / "nvme").mkdir()
    target = tmp_path / "nvme/ha_satimage/data"
    compose = tmp_path / "stack/docker-compose.yml"
    compose.parent.mkdir()
    compose.write_text(yaml.safe_dump({"services": {"ha_satellite": {
        "image": "ghcr.io/bjoernhoefer/ha_satellite:latest", "container_name": "ha_satellite",
        "volumes": [f"{source}:/data", "/mnt:/mnt"],
    }}}))
    (tmp_path / "mount").write_text(str(source))
    (tmp_path / "systemd").mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_UUID": UUID, "STUB_DIR": str(tmp_path), "STUB_LOG": str(tmp_path / "calls.log"),
        "STUB_COMPOSE": str(compose), "STUB_TARGET": str(target), "STUB_PY": sys.executable,
        "SOURCE": str(source), "TARGET": str(target), "SYSTEMD_DIR": str(tmp_path / "systemd"),
    }
    return {"env": env, "source": source, "target": target, "compose": compose, "dir": tmp_path}


def _run(host, *args, **extra):
    return subprocess.run(
        ["bash", str(ROOT / "scripts/ha_nzbpi_switch.sh"), *args],
        env={**host["env"], **extra}, capture_output=True, text=True, timeout=120,
    )


def test_without_run_only_checks(host):
    result = _run(host)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Nothing changed" in result.stdout
    assert not host["target"].exists()
    assert "docker stop" not in (host["dir"] / "calls.log").read_text()


def test_full_switch_and_repeat(host):
    result = _run(host, "--run")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Switch verified" in result.stdout
    assert (host["target"] / "frames/wien/a.png").read_bytes() == b"png" * 50
    assert (host["source"] / "frames/wien/a.png").exists()  # never deleted
    assert (host["source"] / ".ha_satimage-migrated").exists()
    service = yaml.safe_load(host["compose"].read_text())["services"]["ha_satellite"]
    assert service["volumes"][0]["source"] == str(host["target"])
    assert Path(str(host["compose"]) + ".before-nvme").exists()
    unit = (host["dir"] / "systemd/ha-satimage-after-mount.service").read_text()
    assert f"RequiresMountsFor={host['target']}" in unit
    calls = (host["dir"] / "calls.log").read_text()
    assert calls.index("docker stop") < calls.index("compose -p ha_satellite")
    assert "systemctl enable ha-satimage-after-mount.service" in calls

    # Second run: already switched, only verification (+ unit) again.
    again = _run(host, "--run")
    assert again.returncode == 0, again.stderr + again.stdout
    assert "already uses" in again.stdout


def test_wrong_disk_stops_before_anything(host):
    result = _run(host, "--run", STUB_UUID="aaaa-bbbb")
    assert result.returncode == 1
    assert "is the NVMe mounted" in result.stderr
    assert not host["target"].exists()


def test_frames_on_the_sd_card_stop_before_the_switch(host):
    (host["source"] / "config.yaml").write_text("storage:\n  frames_dir: /\n")
    result = _run(host, "--run")
    assert result.returncode == 1
    assert "neither below /data nor /mnt" in result.stderr
    assert not host["target"].exists()


def test_failed_start_reports_without_rollback(host):
    result = _run(host, "--run", STUB_FAIL_UP="1")
    assert result.returncode != 0
    assert "did not complete after the service was stopped" in result.stderr
    assert "manual rollback" in result.stderr
    # Nothing rolled back or deleted automatically; no systemd unit.
    assert (host["source"] / ".ha_satimage-migrated").exists()
    assert (host["target"] / "config.yaml").exists()
    assert not (host["dir"] / "systemd/ha-satimage-after-mount.service").exists()
    json.loads(subprocess.run(
        [sys.executable, str(ROOT / "scripts/ha_storage_migrate.py"), "--source", str(host["source"]),
         "--target", str(host["target"]), "status"],
        env=host["env"], capture_output=True, text=True, check=True,
    ).stdout)


def test_neighbours_with_parent_mounts_pass_but_a_direct_user_stops_before_the_stop(host):
    nvme = str(host["dir"] / "nvme")
    neighbours = [
        {"Name": "/node_exporter", "Config": {"Image": "prom/node-exporter"}, "Mounts": [{"Source": "/"}]},
        {"Name": "/radarr", "Config": {"Image": "linuxserver/radarr"}, "Mounts": [{"Source": nvme}]},
    ]
    result = _run(host, STUB_PS_JSON=json.dumps(neighbours))
    assert result.returncode == 0, result.stderr + result.stdout
    assert '"parent_mounts"' in result.stdout

    backup = {"Name": "/backup", "Config": {"Image": "restic"}, "Mounts": [{"Source": str(host["source"])}]}
    result = _run(host, "--run", STUB_PS_JSON=json.dumps([*neighbours, backup]))
    assert result.returncode == 1
    assert "other containers mount the data (backup)" in result.stderr
    assert "docker stop" not in (host["dir"] / "calls.log").read_text()
    assert not host["target"].exists()
