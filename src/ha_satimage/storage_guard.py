"""Storage guard: refuse to run on the wrong filesystem.

If the data disk (e.g. the NVMe under ``/mnt/data2`` on the Raspberry Pi)
is not mounted, the bind-mount source is just an empty directory on the
host's root filesystem - usually the SD card. Writing there silently would
put all downloads and renders back on the SD card. This module therefore
checks the **identity** of the filesystem behind a path, not only that the
directory exists:

- ``HA_SATIMAGE_REQUIRE_FS_UUID`` (comma list): the filesystem behind
  ``/data`` must be one of these UUIDs. ``none``/``off`` disables the check
  explicitly; unset means disabled (development, tests).
- ``HA_SATIMAGE_FRAMES_FS_UUID``: same for a frame storage location outside
  ``/data`` (unset/empty: the ``/data`` UUIDs; ``none`` disables).
- The UUID is resolved via a read-only bind of the host's
  ``/dev/disk/by-uuid`` (``HA_SATIMAGE_DISK_BY_UUID_DIR``) to a block device
  and compared with the ``/proc/self/mountinfo`` entry of the path (device
  number or mount source).
- ``HA_SATIMAGE_MIN_FREE_MB``: below this free space the check fails
  (startup) or downloads/renders pause (running service).

A path on the container's own root filesystem (overlay, i.e. Docker's data
directory) is always rejected. Nothing is ever created by the checks.

Runs as ``python -m ha_satimage.storage_guard PATH...`` from the entrypoint
on every container start (exit code 78 on failure, before anything is
written) and again inside the app (startup, periodic health job, storage
location changes).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import logging
import os
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

EXIT_STORAGE = 78  # EX_CONFIG
MIGRATED_MARKER = ".ha_satimage-migrated"
INSTANCE_LOCK = ".instance.lock"
DISABLED_VALUES = {"", "none", "off", "false", "0", "disabled"}
DEFAULT_MIN_FREE_MB = 1024
DEFAULT_WARN_FREE_MB = 5120


class StorageGuardError(Exception):
    """The storage is not usable; the message explains what to do."""


@dataclass(frozen=True)
class Settings:
    data_uuids: tuple[str, ...] = ()
    frames_uuids: tuple[str, ...] = ()
    by_uuid_dir: Path = Path("/run/host-disk-by-uuid")
    sys_block_dir: Path = Path("/sys/class/block")
    mountinfo: Path = Path("/proc/self/mountinfo")
    min_free_mb: int = DEFAULT_MIN_FREE_MB
    warn_free_mb: int = DEFAULT_WARN_FREE_MB

    @classmethod
    def from_env(cls, environ=None) -> "Settings":
        env = os.environ if environ is None else environ
        data = _uuid_list(env.get("HA_SATIMAGE_REQUIRE_FS_UUID", ""))
        frames_raw = env.get("HA_SATIMAGE_FRAMES_FS_UUID")
        # Unset or empty: same disk(s) as /data; "none" disables explicitly.
        frames = data if not (frames_raw or "").strip() else _uuid_list(frames_raw)
        return cls(
            data_uuids=data,
            frames_uuids=frames,
            by_uuid_dir=Path(env.get("HA_SATIMAGE_DISK_BY_UUID_DIR", "/run/host-disk-by-uuid")),
            sys_block_dir=Path(env.get("HA_SATIMAGE_SYS_BLOCK_DIR", "/sys/class/block")),
            mountinfo=Path(env.get("HA_SATIMAGE_MOUNTINFO", "/proc/self/mountinfo")),
            min_free_mb=_int(env.get("HA_SATIMAGE_MIN_FREE_MB"), DEFAULT_MIN_FREE_MB),
            warn_free_mb=_int(env.get("HA_SATIMAGE_WARN_FREE_MB"), DEFAULT_WARN_FREE_MB),
        )


def _uuid_list(raw: str) -> tuple[str, ...]:
    if raw.strip().lower() in DISABLED_VALUES:
        return ()
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def _int(raw, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Mount:
    major_minor: str
    root: str
    mountpoint: str
    fstype: str
    source: str


def _unescape(value: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as octal.
    return (
        value.replace("\\040", " ").replace("\\011", "\t")
        .replace("\\012", "\n").replace("\\134", "\\")
    )


def parse_mountinfo(text: str) -> list[Mount]:
    mounts = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        tail = right.split()
        if not sep or len(fields) < 5 or len(tail) < 2:
            continue
        mounts.append(Mount(fields[2], _unescape(fields[3]), _unescape(fields[4]), tail[0], _unescape(tail[1])))
    return mounts


def mount_for(path: Path, mounts: list[Mount]) -> Mount | None:
    """The mount that contains ``path`` (longest mount point, last wins)."""
    resolved = os.path.realpath(path)
    best: Mount | None = None
    for mount in mounts:
        point = mount.mountpoint.rstrip("/") or "/"
        if resolved == point or resolved.startswith(point.rstrip("/") + "/") or point == "/":
            if best is None or len(point) >= len(best.mountpoint.rstrip("/") or "/"):
                best = mount
    return best


def device_for_uuid(fs_uuid: str, settings: Settings) -> tuple[str, str]:
    """(device name, "major:minor") of the filesystem UUID."""
    if not settings.by_uuid_dir.is_dir():
        raise StorageGuardError(
            f"{settings.by_uuid_dir} is missing: mount the host's /dev/disk/by-uuid there "
            "read-only (see docker-compose.yml), or disable the check explicitly with "
            "HA_SATIMAGE_REQUIRE_FS_UUID=none"
        )
    link = settings.by_uuid_dir / fs_uuid
    try:
        target = os.readlink(link)
    except OSError:
        raise StorageGuardError(
            f"Filesystem UUID {fs_uuid} is not present on this host - is the disk connected?"
        ) from None
    name = os.path.basename(target)
    try:
        major_minor = (settings.sys_block_dir / name / "dev").read_text().strip()
    except OSError:
        raise StorageGuardError(f"Block device {name} of UUID {fs_uuid} is not readable in sysfs") from None
    return name, major_minor


@dataclass
class PathReport:
    path: str
    mountpoint: str | None = None
    device: str | None = None
    fstype: str | None = None
    fs_uuid: str | None = None
    free_mb: int | None = None
    total_mb: int | None = None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok}


def check_path(
    path: Path,
    uuids: tuple[str, ...],
    settings: Settings,
    write: bool = True,
) -> PathReport:
    """Check one directory without creating anything."""
    path = Path(path)
    report = PathReport(str(path))
    if not path.is_dir():
        report.problems.append(
            f"{path} does not exist (or is not a directory). It is not created automatically, "
            "so a missing disk mount cannot lead to writes on the system disk - create it on "
            "the right disk first."
        )
        return report
    if (path / MIGRATED_MARKER).exists():
        report.problems.append(
            f"{path} contains {MIGRATED_MARKER}: this is the old data directory that was "
            "migrated to a new location - point the deployment at the new location"
        )
    try:
        mounts = parse_mountinfo(settings.mountinfo.read_text())
    except OSError as exc:
        mounts = []
        report.warnings.append(f"mountinfo not readable: {exc}")
    mount = mount_for(path, mounts)
    if mount is not None:
        report.mountpoint, report.device, report.fstype = mount.mountpoint, mount.source, mount.fstype
        if mount.mountpoint == "/" and mount.fstype in ("overlay", "aufs", "fuse-overlayfs"):
            report.problems.append(
                f"{path} is inside the container's own filesystem (Docker data directory, "
                "usually the system disk) - bind-mount a host directory there"
            )
    if uuids:
        matched = None
        errors = []
        for fs_uuid in uuids:
            try:
                name, major_minor = device_for_uuid(fs_uuid, settings)
            except StorageGuardError as exc:
                errors.append(str(exc))
                continue
            if mount is not None and (
                mount.major_minor == major_minor or os.path.basename(mount.source) == name
            ):
                matched = fs_uuid
                break
            errors.append(
                f"{path} is on {mount.source if mount else '?'} ({mount.major_minor if mount else '?'}), "
                f"not on UUID {fs_uuid} ({name}, {major_minor}) - is the disk mounted on the host?"
            )
        if matched:
            report.fs_uuid = matched
        else:
            report.problems.extend(errors)
    try:
        usage = shutil.disk_usage(path)
        report.free_mb = usage.free // 2**20
        report.total_mb = usage.total // 2**20
    except OSError as exc:
        report.problems.append(f"Free space of {path} unknown: {exc}")
    if report.free_mb is not None:
        if report.free_mb < settings.min_free_mb:
            report.problems.append(
                f"Only {report.free_mb} MiB free on {path} (minimum {settings.min_free_mb} MiB, "
                "HA_SATIMAGE_MIN_FREE_MB)"
            )
        elif report.free_mb < settings.warn_free_mb:
            report.warnings.append(
                f"Only {report.free_mb} MiB free on {path} (warning below {settings.warn_free_mb} MiB)"
            )
    if write and not report.problems:
        probe = path / f".write-test-{uuid.uuid4().hex}"
        try:
            with open(probe, "wb") as handle:
                handle.write(b"ok")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            hint = (
                "disk full" if exc.errno == errno.ENOSPC
                else "read-only filesystem" if exc.errno == errno.EROFS
                else f"permissions: runs as UID {os.getuid()} / GID {os.getgid()}, "
                "check owner on the host or PUID/PGID"
            )
            report.problems.append(f"{path} is not writable ({exc.strerror or exc}; {hint})")
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
    return report


def check_storage(
    data_dir: Path, frames_dir: Path | None = None, settings: Settings | None = None,
    write: bool = True,
) -> list[PathReport]:
    """Check ``/data`` and an external frame location; raise on problems."""
    settings = settings or Settings.from_env()
    reports = [check_path(data_dir, settings.data_uuids, settings, write)]
    if frames_dir is not None and not _inside(frames_dir, data_dir):
        reports.append(check_path(frames_dir, settings.frames_uuids, settings, write))
    problems = [p for r in reports for p in r.problems]
    if problems:
        raise StorageGuardError("; ".join(problems))
    return reports


def _inside(path: Path, base: Path) -> bool:
    path, base = Path(os.path.abspath(path)), Path(os.path.abspath(base))
    return path == base or base in path.parents


def check_new_frames_dir(path: Path, data_dir: Path, settings: Settings | None = None) -> None:
    """Validate a storage location chosen in the UI before it is created.

    Only the last path component may be missing, and its parent must pass
    the identity check - so ``/mnt/<disk>/ha_satimage`` can be created on a
    mounted disk, but never below an unmounted mount point.
    """
    settings = settings or Settings.from_env()
    path = Path(path)
    if _inside(path, data_dir):
        return
    anchor = path if path.is_dir() else path.parent
    report = check_path(anchor, settings.frames_uuids, settings, write=False)
    if not report.ok:
        raise StorageGuardError("; ".join(report.problems))


def ensure_temp_dir(data_dir: Path, environ=None) -> str:
    """Make sure temporary files land where ``TMPDIR`` says.

    Python's ``tempfile`` silently falls back to ``/tmp`` (the container
    filesystem, i.e. the system disk) if ``TMPDIR`` does not exist. A
    ``TMPDIR`` inside the checked data directory is created; any other
    missing one is an error.
    """
    import tempfile

    env = os.environ if environ is None else environ
    wanted = env.get("TMPDIR")
    if not wanted:
        return tempfile.gettempdir()
    path = Path(wanted)
    if not path.is_dir():
        if not _inside(path, data_dir):
            raise StorageGuardError(f"TMPDIR {path} does not exist")
        path.mkdir(mode=0o700, exist_ok=True)
    tempfile.tempdir = None  # re-evaluate after creating it
    actual = tempfile.gettempdir()
    if os.path.realpath(actual) != os.path.realpath(path):
        raise StorageGuardError(f"TMPDIR {path} is not usable (tempfile uses {actual})")
    return actual


# -- Single instance ------------------------------------------------------------------
_lock_handles: dict[str, object] = {}


def acquire_instance_lock(data_dir: Path) -> None:
    """Exclusive lock on the data directory for the life of the process.

    Prevents an old (``ha_satellite``) and a new container from writing the
    same data at the same time (flock works across bind mounts on one host).
    """
    key = os.path.abspath(data_dir)
    if key in _lock_handles:
        return
    path = Path(data_dir) / INSTANCE_LOCK
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        holder = handle.read().strip() or "unknown"
        handle.close()
        raise StorageGuardError(
            f"{data_dir} is already used by another running instance ({holder}) - "
            "stop the other container first (old name ha_satellite?)"
        ) from None
    handle.seek(0)
    handle.truncate()
    handle.write(f"host={os.uname().nodename} pid={os.getpid()}\n")
    handle.flush()
    _lock_handles[key] = handle


def release_instance_lock(data_dir: Path) -> None:
    handle = _lock_handles.pop(os.path.abspath(data_dir), None)
    if handle is not None:
        handle.close()


# -- CLI (entrypoint) -------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the storage of ha_satimage")
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("frames_dir", type=Path, nargs="?")
    parser.add_argument("--no-write", action="store_true", help="skip the write test")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    try:
        reports = check_storage(args.data_dir, args.frames_dir, settings, write=not args.no_write)
    except StorageGuardError as exc:
        print(f"ha_satimage storage check FAILED: {exc}", file=sys.stderr)
        print(
            "Refusing to start so nothing is written to the wrong disk. "
            "Fix the mount/permissions, then start the container again.",
            file=sys.stderr,
        )
        return EXIT_STORAGE
    for report in reports:
        identity = f"UUID {report.fs_uuid}" if report.fs_uuid else "identity check disabled"
        print(
            f"ha_satimage storage check ok: {report.path} on {report.device} "
            f"({report.fstype}, {identity}), {report.free_mb} MiB free",
            file=sys.stderr,
        )
        for warning in report.warnings:
            print(f"ha_satimage storage warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
