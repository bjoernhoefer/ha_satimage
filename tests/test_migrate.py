"""Migration tool scripts/ha_storage_migrate.py (repeatable, interruptible, no data loss)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from collections import namedtuple
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ha_storage_migrate", ROOT / "scripts" / "ha_storage_migrate.py")
migrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate)

UUID = "e16ccc22-790c-4f89-97f5-b7caca060bbd"
pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync missing")


@pytest.fixture
def env(tmp_path, monkeypatch):
    source = tmp_path / "home" / "ha_satellite" / "data"
    (source / "frames" / "wien").mkdir(parents=True)
    (source / "cache").mkdir()
    (source / "logs").mkdir()
    (source / "config.yaml").write_text("regions: []\n")
    (source / "source_sync.json").write_text("{}")
    (source / "frames" / "wien" / "_index.json").write_text('[{"filename": "a.png"}]')
    (source / "frames" / "wien" / "a.png").write_bytes(b"png" * 100)
    (source / "cache" / "product.nat").write_bytes(os.urandom(4096))
    (source / ".instance.lock").write_text("pid=1")
    os.symlink("frames/wien/a.png", source / "latest-link")
    target = tmp_path / "nvme" / "ha_satimage" / "data"
    (tmp_path / "nvme").mkdir()
    state = {"uuid": UUID, "containers": []}
    monkeypatch.setattr(migrate, "fs_uuid_of", lambda path: state["uuid"])
    monkeypatch.setattr(migrate, "mount_users", lambda paths: {
        "direct": list(state["containers"]), "parent": list(state.get("parent", [])), "own_parent": [],
    })
    monkeypatch.setattr(migrate.Migration, "check_root", lambda self: None)
    Env = namedtuple("Env", "source target state migration")
    return Env(source, target, state, migrate.Migration(source, target, UUID, reserve_mb=0))


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def test_full_migration_with_running_service_then_finalize(env):
    info = env.migration.preflight()
    assert info["phase"] == "new" and info["containers"] == []
    assert not env.target.exists()  # preflight creates nothing

    env.migration.copy()
    assert env.migration.phase == "copying"
    assert not (env.target / ".instance.lock").exists()
    # The service keeps writing: new frame, deleted cache file.
    (env.source / "frames" / "wien" / "b.png").write_bytes(b"new")
    (env.source / "cache" / "product.nat").unlink()
    env.migration.copy()  # repeatable
    assert (env.target / "cache" / "product.nat").exists()  # copy never deletes

    env.state["containers"] = ["ha_satellite"]
    with pytest.raises(migrate.MigrationError, match="ha_satellite"):
        env.migration.finalize()
    assert not (env.source / migrate.MARKER).exists()

    env.state["containers"] = []
    env.migration.finalize()
    assert env.migration.phase == "finalized"
    expected = {k: v for k, v in _files(env.source).items() if k not in (".instance.lock", migrate.MARKER)}
    assert _files(env.target) == expected
    assert os.readlink(env.target / "latest-link") == "frames/wien/a.png"
    marker = json.loads((env.source / migrate.MARKER).read_text())
    assert marker["migrated_to"] == str(env.target)
    # Source untouched apart from the marker.
    assert (env.source / "config.yaml").exists() and (env.source / "frames" / "wien" / "b.png").exists()

    # No second copy over data the new instance writes.
    with pytest.raises(migrate.MigrationError, match="Already finalized"):
        env.migration.copy()
    with pytest.raises(migrate.MigrationError, match="Already finalized"):
        env.migration.finalize()


def test_interrupted_finalize_can_be_repeated(env, monkeypatch):
    env.migration.copy()
    (env.source / "frames" / "wien" / "c.png").write_bytes(b"c")
    real_run = migrate.run
    calls = {"n": 0}

    def interrupted(args, **kwargs):
        if args[0] == "rsync" and "--delete" in args and "--dry-run" not in args and calls["n"] == 0:
            calls["n"] += 1
            raise migrate.MigrationError("rsync killed (power loss)")
        return real_run(args, **kwargs)

    monkeypatch.setattr(migrate, "run", interrupted)
    with pytest.raises(migrate.MigrationError, match="power loss"):
        env.migration.finalize()
    assert env.migration.phase == "copying"
    assert not (env.source / migrate.MARKER).exists()
    env.migration.finalize()
    assert (env.target / "frames" / "wien" / "c.png").read_bytes() == b"c"
    assert (env.source / migrate.MARKER).exists()


def test_marker_is_restored_if_finalize_was_cut_after_the_state(env):
    env.migration.copy()
    env.migration.finalize()
    (env.source / migrate.MARKER).unlink()
    with pytest.raises(migrate.MigrationError, match="Already finalized"):
        env.migration.finalize()
    assert (env.source / migrate.MARKER).exists()


def test_verify_detects_silently_changed_config(env):
    env.migration.copy()
    env.migration.finalize()
    config = env.target / "config.yaml"
    stat = config.stat()
    config.write_text("regions: [x\n")  # same size and time: rsync's quick check misses it
    os.utime(config, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(migrate.MigrationError, match="Checksum mismatch: config.yaml"):
        env.migration.verify()
    (env.target / "frames" / "wien" / "a.png").unlink()
    with pytest.raises(migrate.MigrationError, match="differs"):
        env.migration.verify()


def test_foreign_target_is_never_overwritten(env):
    env.target.mkdir(parents=True)
    (env.target / "config.yaml").write_text("someone else\n")
    with pytest.raises(migrate.MigrationError, match="not empty"):
        env.migration.preflight()
    with pytest.raises(migrate.MigrationError, match="not empty"):
        env.migration.copy()
    assert (env.target / "config.yaml").read_text() == "someone else\n"


def test_unmounted_disk_creates_nothing(env):
    env.state["uuid"] = "11111111-2222-3333-4444-555555555555"  # root filesystem
    with pytest.raises(migrate.MigrationError, match="is the disk mounted"):
        env.migration.copy()
    assert not env.target.parent.exists()


def test_not_enough_space(env, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(migrate.shutil, "disk_usage", lambda p: Usage(100, 100, 10))
    with pytest.raises(migrate.MigrationError, match="Not enough free space"):
        env.migration.copy()
    assert not env.target.exists()


def test_already_migrated_source_is_refused(env):
    (env.source / migrate.MARKER).write_text("{}")
    with pytest.raises(migrate.MigrationError, match="already migrated"):
        env.migration.copy()


def test_rollback_keeps_data_written_after_the_switch(env):
    env.migration.copy()
    env.migration.finalize()
    (env.target / "frames" / "wien" / "new-after-switch.png").write_bytes(b"new")
    (env.target / "config.yaml").write_text("regions: [changed]\n")
    env.state["containers"] = ["ha_satellite"]
    with pytest.raises(migrate.MigrationError, match="stop them"):
        env.migration.rollback()
    env.state["containers"] = []
    env.migration.rollback()
    assert (env.source / "frames" / "wien" / "new-after-switch.png").read_bytes() == b"new"
    assert (env.source / "config.yaml").read_text() == "regions: [changed]\n"
    assert not (env.source / migrate.MARKER).exists()
    assert (env.target / "config.yaml").exists()  # target kept
    assert env.migration.phase == "rolled_back"
    # A new migration round is possible afterwards.
    env.migration.copy()
    env.migration.finalize()
    assert env.migration.phase == "finalized"


def _docker_ps(monkeypatch, containers):
    """Fake ``docker ps``/``docker inspect`` with (name, image, [mount sources])."""
    details = [
        {"Name": f"/{name}", "Config": {"Image": image}, "Mounts": [{"Source": src} for src in sources]}
        for name, image, sources in containers
    ]

    real_run = migrate.run

    def fake_run(cmd, **kwargs):
        if cmd[0] != "docker":
            return real_run(cmd, **kwargs)
        out = "\n".join(str(i) for i in range(len(details))) if cmd[:2] == ["docker", "ps"] else json.dumps(details)
        return namedtuple("Result", "stdout")(out)

    monkeypatch.setattr(migrate, "run", fake_run)


def _neighbours(env):
    """Containers as on nzbpi; ``nvme`` plays /mnt/data2, its parent /mnt."""
    nvme = str(env.target.parent.parent)
    return [
        ("filebrowser", "filebrowser/filebrowser", [nvme]),
        ("node_exporter", "prom/node-exporter", ["/"]),
        ("radarr", "linuxserver/radarr", [nvme, nvme + "/radarr-config"]),
        ("sabnzbd", "linuxserver/sabnzbd", [nvme]),
    ]


def test_broad_parent_mounts_of_other_containers_do_not_block_finalize(env, monkeypatch):
    """Regression nzbpi: /, /mnt/data2 of unrelated containers blocked finalize."""
    monkeypatch.undo()  # real mount_users, with a fake docker
    monkeypatch.setattr(migrate, "fs_uuid_of", lambda path: UUID)
    monkeypatch.setattr(migrate.Migration, "check_root", lambda self: None)
    _docker_ps(monkeypatch, _neighbours(env))
    paths = [env.source, env.target]
    users = migrate.mount_users(paths)
    assert users == {"direct": [], "parent": ["filebrowser", "node_exporter", "radarr", "sabnzbd"],
                     "own_parent": []}
    assert migrate.containers_using(paths) == []
    env.migration.copy()
    env.migration.finalize()
    assert env.migration.phase == "finalized"


def test_own_instance_or_direct_mounts_still_block(env, monkeypatch):
    monkeypatch.undo()
    _docker_ps(monkeypatch, [
        *_neighbours(env),
        ("ha_satellite", "ghcr.io/bjoernhoefer/ha_satellite:latest", [str(env.source)]),
    ])
    assert migrate.containers_using([env.source, env.target]) == ["ha_satellite"]
    # A new instance that only mounts /mnt (parent of the target) also blocks.
    mnt = str(env.target.parent.parent.parent)
    _docker_ps(monkeypatch, [("ha_satimage_new", "ghcr.io/bjoernhoefer/ha_satimage:1.11.0", [mnt])])
    assert migrate.containers_using([env.source, env.target]) == ["ha_satimage_new"]
    # Any container mounting the data itself or a path inside it blocks.
    _docker_ps(monkeypatch, [("backup", "restic", [str(env.target / "frames")])])
    assert migrate.containers_using([env.source, env.target]) == ["backup"]
    with pytest.raises(migrate.MigrationError, match="backup"):
        env.migration.check_stopped()


def test_preflight_reports_parent_mounts_separately(env):
    env.state["parent"] = ["node_exporter"]
    info = env.migration.preflight()
    assert info["containers"] == [] and info["parent_mounts"] == ["node_exporter"]


def test_source_and_target_must_not_overlap(tmp_path):
    with pytest.raises(migrate.MigrationError):
        migrate.Migration(tmp_path / "data", tmp_path / "data" / "new", UUID)


def _compose(**overrides) -> dict:
    service = {
        "volumes": [
            {"type": "bind", "source": "/mnt/data2/ha_satimage/data", "target": "/data",
             "bind": {"create_host_path": False}},
            {"type": "bind", "source": "/mnt", "target": "/mnt", "bind": {"create_host_path": False}},
            {"type": "bind", "source": "/dev/disk/by-uuid", "target": "/run/host-disk-by-uuid",
             "read_only": True, "bind": {"create_host_path": False}},
        ],
        "environment": {"HA_SATIMAGE_REQUIRE_FS_UUID": UUID},
        "logging": {"driver": "json-file", "options": {"max-size": "2m", "max-file": "2"}},
    }
    service.update(overrides)
    return {"services": {"ha_satellite": service}}


def test_compose_check():
    target = "/mnt/data2/ha_satimage/data"
    assert migrate.check_compose_config(_compose(), target, UUID) == []
    old = _compose(volumes=[{"type": "bind", "source": "/home/bjoern/ha_satellite/data",
                             "target": "/data", "bind": {"create_host_path": True}}],
                   environment=[], logging={})
    problems = " | ".join(migrate.check_compose_config(old, target, UUID, "ha_satellite"))
    for expected in ("comes from /home/bjoern", "create_host_path", "by-uuid",
                     "HA_SATIMAGE_REQUIRE_FS_UUID", "size-limited"):
        assert expected in problems
    short = _compose(volumes=["/home/x:/data"])
    assert "long-syntax" in migrate.check_compose_config(short, target, UUID)[0]


HOST_COMPOSE = """\
services:
  ha_satellite:
    image: ghcr.io/bjoernhoefer/ha_satellite:latest
    container_name: ha_satellite
    restart: unless-stopped
    ports: ["6060:6060"]
    mem_limit: 1700003840
    volumes:
      - /home/bjoern/ha_satellite/data:/data
      - /mnt:/mnt
      - cache:/cache
    environment:
      - PUID=1000
      - EUMETSAT_CONSUMER_KEY=${EUMETSAT_CONSUMER_KEY:-}
volumes:
  cache: {}
"""
TARGET = "/mnt/data2/ha_satimage/data"


def _apply(tmp_path, text=HOST_COMPOSE, **kwargs):
    yaml = pytest.importorskip("yaml")
    path = tmp_path / "docker-compose.yml"
    path.write_text(text)
    os.chmod(path, 0o640)
    changed = migrate.apply_compose(path, TARGET, UUID, **kwargs)
    return path, changed, yaml.safe_load(path.read_text())


def test_apply_compose_switches_only_the_data_location(tmp_path):
    path, changed, document = _apply(tmp_path)
    assert changed
    service = document["services"]["ha_satellite"]
    volumes = {v["target"] if isinstance(v, dict) else v.split(":")[1]: v for v in service["volumes"]}
    assert volumes["/data"] == {"type": "bind", "source": TARGET, "target": "/data",
                                "bind": {"create_host_path": False}}
    assert volumes["/mnt"]["source"] == "/mnt" and volumes["/mnt"]["bind"]["create_host_path"] is False
    assert volumes["/run/host-disk-by-uuid"]["read_only"] is True
    assert volumes["/cache"] == "cache:/cache"  # named volume untouched
    assert f"HA_SATIMAGE_REQUIRE_FS_UUID={UUID}" in service["environment"]
    assert "EUMETSAT_CONSUMER_KEY=${EUMETSAT_CONSUMER_KEY:-}" in service["environment"]
    assert service["logging"] == {"driver": "json-file", "options": {"max-size": "2m", "max-file": "2"}}
    # Everything else is kept (name, image, limit, port).
    assert service["container_name"] == "ha_satellite" and service["mem_limit"] == 1700003840
    assert service["image"].endswith("ha_satellite:latest") and service["ports"] == ["6060:6060"]
    # Original kept, permissions kept, result passes the Compose check.
    assert (tmp_path / "docker-compose.yml.before-nvme").read_text() == HOST_COMPOSE
    assert path.stat().st_mode & 0o777 == 0o640
    assert migrate.check_compose_config(document, TARGET, UUID) == []


def test_apply_compose_is_idempotent_and_keeps_the_first_backup(tmp_path):
    path, _, first = _apply(tmp_path)
    assert migrate.apply_compose(path, TARGET, UUID) is False
    assert (tmp_path / "docker-compose.yml.before-nvme").read_text() == HOST_COMPOSE
    import yaml

    assert yaml.safe_load(path.read_text()) == first


def test_apply_compose_sets_the_frames_uuid_for_another_disk(tmp_path):
    other = "11111111-2222-3333-4444-555555555555"
    _, _, document = _apply(tmp_path, frames_uuid=other)
    environment = document["services"]["ha_satellite"]["environment"]
    assert f"HA_SATIMAGE_FRAMES_FS_UUID={other}" in environment


def test_apply_compose_with_mapping_environment_and_existing_log_limit(tmp_path):
    text = HOST_COMPOSE.replace(
        "    environment:\n      - PUID=1000\n      - EUMETSAT_CONSUMER_KEY=${EUMETSAT_CONSUMER_KEY:-}\n",
        "    environment:\n      PUID: 1000\n      HA_SATIMAGE_REQUIRE_FS_UUID: none\n"
        "    logging:\n      options:\n        max-size: 5m\n",
    )
    _, _, document = _apply(tmp_path, text=text)
    service = document["services"]["ha_satellite"]
    assert service["environment"] == {"PUID": 1000, "HA_SATIMAGE_REQUIRE_FS_UUID": UUID}
    assert service["logging"]["options"] == {"max-size": "5m", "max-file": "2"}


def test_apply_compose_refuses_ambiguous_or_missing_service(tmp_path):
    two = HOST_COMPOSE.replace("volumes:\n  cache: {}\n", "") + (
        "  other:\n    image: x\n    volumes:\n      - /srv:/data\n"
    )
    with pytest.raises(migrate.MigrationError, match="exactly one service"):
        _apply(tmp_path, text=two)
    assert not (tmp_path / "docker-compose.yml.before-nvme").exists()
    with pytest.raises(migrate.MigrationError, match="not defined"):
        _apply(tmp_path, service="nope")


def test_compose_v238_omits_false_create_host_path():
    """`docker compose config` (v2.38) prints "bind": {} for create_host_path: false."""
    target = "/mnt/data2/ha_satimage/data"
    effective = _compose()
    for volume in effective["services"]["ha_satellite"]["volumes"]:
        volume["bind"] = {}
    assert migrate.check_compose_config(effective, target, UUID) == []
    # Short syntax is normalized to create_host_path: true and still fails.
    effective["services"]["ha_satellite"]["volumes"][0]["bind"] = {"create_host_path": True}
    assert any("create_host_path" in p for p in migrate.check_compose_config(effective, target, UUID))


def test_compose_file_must_say_create_host_path_false_explicitly():
    written = _compose()
    assert migrate.check_compose_file(written) == []
    written["services"]["ha_satellite"]["volumes"][0]["bind"] = {}
    assert migrate.check_compose_file(written)
    short = {"services": {"ha_satellite": {"volumes": ["/x:/data"]}}}
    assert migrate.check_compose_file(short)
