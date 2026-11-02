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
    monkeypatch.setattr(migrate, "containers_using", lambda paths: list(state["containers"]))
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
