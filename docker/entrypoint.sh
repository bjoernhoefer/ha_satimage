#!/usr/bin/env bash
# Starts as root by default (Docker default), because a bind-mounted
# /data volume overrides the ownership set in the image - a "chown" in the
# Dockerfile alone is therefore not enough. This script fixes the ownership
# at runtime and then runs the actual application with reduced privileges
# (mambauser).
#
# Runs on every container start, including Docker's automatic restart after
# a host reboot. Before anything is created or chowned it checks that /data
# (and an external frame storage location) really is the expected disk
# (ha_satimage.storage_guard: filesystem UUID, free space, write test) and
# exits with code 78 otherwise - nothing is ever written to a directory on
# the system disk that merely has the same name.
#
# PUID/PGID (optional): UID/GID the service runs as. Should match the owner
# of the host directories images are written to, otherwise changing the
# storage location in the web UI fails with "not writable".
set -euo pipefail

DATA_DIR="${HA_SATIMAGE_DATA_DIR:-/data}"
PY=/usr/local/bin/python3

configured_frames_dir() {
    "$PY" - <<'PY' 2>/dev/null || true
import os, yaml
path = os.environ.get("HA_SATIMAGE_CONFIG", "/data/config.yaml")
try:
    with open(path, encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    print((config.get("storage") or {}).get("frames_dir") or "")
except OSError:
    pass
PY
}

if [ "$(id -u)" = "0" ]; then
    frames_dir="$(configured_frames_dir)"
    # Identity/free space only as root; the write test as the service user follows below.
    "$PY" -m ha_satimage.storage_guard --no-write "$DATA_DIR" ${frames_dir:+"$frames_dir"} || exit 78

    if [ -n "${PGID:-}" ] && [ "$PGID" != "$(id -g mambauser)" ]; then
        groupmod -o -g "$PGID" mambauser || echo "WARNING: could not set PGID=$PGID" >&2
    fi
    if [ -n "${PUID:-}" ] && [ "$PUID" != "$(id -u mambauser)" ]; then
        usermod -o -u "$PUID" mambauser || echo "WARNING: could not set PUID=$PUID" >&2
    fi
    uid="$(id -u mambauser)"
    gid="$(id -g mambauser)"

    # Recursive chown only if the top level does not match (first start or
    # PUID/PGID change) - not a metadata walk over all frames on every start.
    fix_owner() {
        local dir="$1"
        if [ "$(stat -c %u:%g "$dir")" != "$uid:$gid" ]; then
            echo "Adjusting ownership of $dir to $uid:$gid" >&2
            chown -R "$uid:$gid" "$dir" || echo "WARNING: could not adjust ownership of $dir" >&2
        fi
    }
    fix_owner "$DATA_DIR"
    if [ -n "$frames_dir" ] && [ -d "$frames_dir" ]; then
        fix_owner "$frames_dir"
    fi
    # Temporary files (TMPDIR) live on the data disk, not in the container
    # filesystem; leftovers of an aborted run are removed at start - only
    # in the dedicated <data>/tmp directory, never in another data folder.
    if [ "${TMPDIR:-}" = "$DATA_DIR/tmp" ]; then
        install -d -m 0700 -o "$uid" -g "$gid" "$TMPDIR"
        find "$TMPDIR" -mindepth 1 -delete 2>/dev/null || true
    fi

    setpriv --reuid="$uid" --regid="$gid" --init-groups \
        "$PY" -m ha_satimage.storage_guard "$DATA_DIR" ${frames_dir:+"$frames_dir"} || exit 78

    exec setpriv --reuid=mambauser --regid=mambauser --init-groups \
        /usr/local/bin/_entrypoint.sh "$@"
fi

"$PY" -m ha_satimage.storage_guard "$DATA_DIR" || exit 78
exec /usr/local/bin/_entrypoint.sh "$@"
