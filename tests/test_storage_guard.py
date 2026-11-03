"""Storage guard: mount identity, free space, rights, instance lock, TMPDIR."""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import tempfile
from collections import namedtuple
from pathlib import Path

import pytest

from ha_satimage import storage_guard
from ha_satimage.storage_guard import (
    EXIT_STORAGE,
    MIGRATED_MARKER,
    Settings,
    StorageGuardError,
    check_new_frames_dir,
    check_path,
    check_storage,
)

NVME_UUID = "e16ccc22-790c-4f89-97f5-b7caca060bbd"
SD_UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def host(tmp_path):
    """Fake host: NVMe mounted at <tmp>/nvme, SD card as root filesystem."""
    nvme = tmp_path / "nvme"
    data = nvme / "ha_satimage" / "data"
    data.mkdir(parents=True)
    sdcard_dir = tmp_path / "sd" / "data"
    sdcard_dir.mkdir(parents=True)
    by_uuid = tmp_path / "by-uuid"
    by_uuid.mkdir()
    (by_uuid / NVME_UUID).symlink_to("../../nvme0n1p1")
    (by_uuid / SD_UUID).symlink_to("../../mmcblk0p2")
    sys_block = tmp_path / "sys-block"
    for name, dev in (("nvme0n1p1", "259:1"), ("mmcblk0p2", "179:2")):
        (sys_block / name).mkdir(parents=True)
        (sys_block / name / "dev").write_text(dev + "\n")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 1 179:2 / / rw,noatime shared:1 - ext4 /dev/mmcblk0p2 rw\n"
        f"40 22 259:1 / {nvme} rw,relatime shared:2 - ext4 /dev/nvme0n1p1 rw\n"
    )
    settings = Settings(
        data_uuids=(NVME_UUID,), frames_uuids=(NVME_UUID,), by_uuid_dir=by_uuid,
        sys_block_dir=sys_block, mountinfo=mountinfo, min_free_mb=1, warn_free_mb=2,
    )
    Host = namedtuple("Host", "settings data sdcard nvme mountinfo tmp")
    return Host(settings, data, sdcard_dir, nvme, mountinfo, tmp_path)


def test_settings_from_env():
    settings = Settings.from_env({
        "HA_SATIMAGE_REQUIRE_FS_UUID": f" {NVME_UUID.upper()} ,{SD_UUID}",
        "HA_SATIMAGE_MIN_FREE_MB": "500",
    })
    assert settings.data_uuids == (NVME_UUID, SD_UUID)
    assert settings.frames_uuids == settings.data_uuids  # inherited
    assert settings.min_free_mb == 500
    assert Settings.from_env({"HA_SATIMAGE_REQUIRE_FS_UUID": "none"}).data_uuids == ()
    assert Settings.from_env({}).data_uuids == ()
    disabled_frames = Settings.from_env({
        "HA_SATIMAGE_REQUIRE_FS_UUID": NVME_UUID, "HA_SATIMAGE_FRAMES_FS_UUID": "off",
    })
    assert disabled_frames.frames_uuids == ()
    assert Settings.from_env({"HA_SATIMAGE_MIN_FREE_MB": "x"}).min_free_mb == 1024


def test_mounted_nvme_passes(host):
    report = check_path(host.data, (NVME_UUID,), host.settings)
    assert report.ok, report.problems
    assert report.fs_uuid == NVME_UUID
    assert report.device == "/dev/nvme0n1p1"
    assert not list(host.data.iterdir())  # write probe removed


def test_missing_mount_rejects_directory_on_sd_card(host):
    # Unmounted disk: the same path now belongs to the root filesystem.
    host.mountinfo.write_text("22 1 179:2 / / rw - ext4 /dev/mmcblk0p2 rw\n")
    report = check_path(host.data, (NVME_UUID,), host.settings)
    assert not report.ok
    assert "not on UUID" in report.problems[0] and "mounted" in report.problems[0]
    assert not list(host.data.iterdir())  # no write test on the wrong disk


def test_wrong_filesystem_is_rejected(host):
    report = check_path(host.sdcard, (NVME_UUID,), host.settings)
    assert not report.ok
    assert "/dev/mmcblk0p2" in report.problems[0]


def test_missing_disk_and_missing_by_uuid_mount(host):
    shutil.rmtree(host.settings.by_uuid_dir / ".." / "by-uuid")
    with pytest.raises(StorageGuardError, match="by-uuid"):
        check_storage(host.data, settings=host.settings)
    host.settings.by_uuid_dir.mkdir()
    with pytest.raises(StorageGuardError, match="not present on this host"):
        check_storage(host.data, settings=host.settings)


def test_missing_directory_is_never_created(host):
    missing = host.nvme / "nope" / "data"
    with pytest.raises(StorageGuardError, match="not created automatically"):
        check_storage(missing, settings=host.settings)
    assert not missing.parent.exists()


def test_overlay_root_is_rejected_even_without_uuid(host):
    host.mountinfo.write_text("22 1 0:50 / / rw - overlay overlay rw\n")
    report = check_path(host.sdcard, (), host.settings)
    assert any("container's own filesystem" in p for p in report.problems)


def test_migrated_old_directory_is_rejected(host):
    (host.data / MIGRATED_MARKER).write_text("{}")
    with pytest.raises(StorageGuardError, match="migrated"):
        check_storage(host.data, settings=host.settings)


def test_full_disk_is_rejected(host, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(storage_guard.shutil, "disk_usage", lambda p: Usage(10 * 2**30, 10 * 2**30, 0))
    with pytest.raises(StorageGuardError, match="Only 0 MiB free"):
        check_storage(host.data, settings=host.settings)
    monkeypatch.setattr(
        storage_guard.shutil, "disk_usage", lambda p: Usage(10 * 2**30, 0, int(1.5 * 2**20))
    )
    report = check_storage(host.data, settings=host.settings)[0]
    assert report.warnings and "warning below" in report.warnings[0]


@pytest.mark.parametrize(
    ("code", "hint"),
    [(errno.EACCES, "permissions"), (errno.ENOSPC, "disk full"), (errno.EROFS, "read-only")],
)
def test_write_failures_are_explained(host, monkeypatch, code, hint):
    def failing_open(*args, **kwargs):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(storage_guard, "open", failing_open, raising=False)
    with pytest.raises(StorageGuardError, match=hint):
        check_storage(host.data, settings=host.settings)
    # --no-write (entrypoint as root) skips the probe.
    assert check_storage(host.data, settings=host.settings, write=False)[0].ok


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_real_permission_problem(host):
    host.data.chmod(0o500)
    try:
        with pytest.raises(StorageGuardError, match="not writable"):
            check_storage(host.data, settings=host.settings)
    finally:
        host.data.chmod(0o700)


def test_external_frames_dir_is_checked_separately(host):
    frames = host.sdcard
    with pytest.raises(StorageGuardError, match="mmcblk0p2"):
        check_storage(host.data, frames, settings=host.settings)
    # Inside /data: covered by the /data check.
    inside = host.data / "frames"
    inside.mkdir()
    assert len(check_storage(host.data, inside, settings=host.settings)) == 1


def test_new_frames_dir_needs_parent_on_the_right_disk(host):
    check_new_frames_dir(host.nvme / "frames-new", host.data, host.settings)
    with pytest.raises(StorageGuardError):
        check_new_frames_dir(host.sdcard / "frames", host.data, host.settings)
    with pytest.raises(StorageGuardError, match="does not exist"):
        check_new_frames_dir(host.nvme / "a" / "b", host.data, host.settings)


def test_cli_exit_codes(host, monkeypatch, capsys):
    env = {
        "HA_SATIMAGE_REQUIRE_FS_UUID": NVME_UUID,
        "HA_SATIMAGE_DISK_BY_UUID_DIR": str(host.settings.by_uuid_dir),
        "HA_SATIMAGE_SYS_BLOCK_DIR": str(host.settings.sys_block_dir),
        "HA_SATIMAGE_MOUNTINFO": str(host.mountinfo),
        "HA_SATIMAGE_MIN_FREE_MB": "1",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert storage_guard.main([str(host.data)]) == 0
    assert "storage check ok" in capsys.readouterr().err
    assert storage_guard.main([str(host.sdcard)]) == EXIT_STORAGE
    assert "FAILED" in capsys.readouterr().err


def test_instance_lock_blocks_second_instance(tmp_path):
    other = open(tmp_path / storage_guard.INSTANCE_LOCK, "a+")
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(StorageGuardError, match="another running instance"):
            storage_guard.acquire_instance_lock(tmp_path)
    finally:
        other.close()
    storage_guard.acquire_instance_lock(tmp_path)
    try:
        assert "pid=" in (tmp_path / storage_guard.INSTANCE_LOCK).read_text()
        probe = open(tmp_path / storage_guard.INSTANCE_LOCK, "a+")
        with pytest.raises(OSError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        probe.close()
    finally:
        storage_guard.release_instance_lock(tmp_path)


def test_temp_dir_inside_data_is_created_never_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", None)
    try:
        wanted = tmp_path / "tmp"
        monkeypatch.setenv("TMPDIR", str(wanted))
        assert storage_guard.ensure_temp_dir(tmp_path) == str(wanted)
        assert wanted.is_dir()
        assert Path(tempfile.mkstemp()[1]).parent == wanted
        outside = tmp_path.parent / "elsewhere-not-created"
        with pytest.raises(StorageGuardError, match="does not exist"):
            storage_guard.ensure_temp_dir(tmp_path / "data", {"TMPDIR": str(outside)})
        assert not outside.exists()
    finally:
        tempfile.tempdir = None
