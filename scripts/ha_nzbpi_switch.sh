#!/usr/bin/env bash
# Switch the ha_satimage deployment on the Raspberry Pi "nzbpi" from the SD
# card to the NVMe in one controlled run (approvals B + C, docs/storage.md):
#
#   1. checks (root, tools, NVMe mount by UUID, container + Compose file)
#   2. preflight + copy while the service keeps running (idle I/O priority)
#   3. host Compose file: switched to the NVMe path (backup *.before-nvme),
#      validated - all before the service is stopped
#   4. service stopped, final sync + verification + marker in the old dir
#   5. service started from the NVMe, success verified
#   6. only if everything succeeded: systemd unit for a late NVMe mount
#
# Nothing is deleted, nothing is rolled back automatically: if a step after
# the stop fails, the script stops and prints the state and the commands
# for a manual rollback (last resort, docs/storage.md "Rollback").
#
# Usage (as root on nzbpi, from a checkout of the repository):
#   sudo bash scripts/ha_nzbpi_switch.sh            # dry check only
#   sudo bash scripts/ha_nzbpi_switch.sh --run      # perform the switch
# Environment: CONTAINER (ha_satellite), SOURCE, TARGET, EXPECT_UUID,
#   SKIP_SYSTEMD=1 (do not install the unit).
set -euo pipefail

CONTAINER=${CONTAINER:-ha_satellite}
SOURCE=${SOURCE:-/home/bjoern/ha_satellite/data}
TARGET=${TARGET:-/mnt/data2/ha_satimage/data}
EXPECT_UUID=${EXPECT_UUID:-e16ccc22-790c-4f89-97f5-b7caca060bbd}
UNIT=ha-satimage-after-mount.service
SYSTEMD_DIR=${SYSTEMD_DIR:-/etc/systemd/system}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
MIGRATE=(python3 "$HERE/ha_storage_migrate.py" --source "$SOURCE" --target "$TARGET" --expect-uuid "$EXPECT_UUID")
RUN=0
[ "${1:-}" = "--run" ] && RUN=1

step() { printf '\n=== %s\n' "$*"; }
fail() { printf '\nSTOP: %s\n' "$*" >&2; exit 1; }
STOPPED=0
after_stop_failure() {
    local code=$?
    [ "$STOPPED" = 1 ] && [ "$code" != 0 ] || return 0
    cat >&2 <<EOF

!!! The switch did not complete after the service was stopped (exit $code).
Nothing was deleted. Current state:
  $("${MIGRATE[@]}" status 2>/dev/null | tr '\n' ' ')
Compose backup: $FILES.before-nvme
Options:
  - fix the reported problem and run this script again with --run
    (every step is repeatable), or
  - last resort, manual rollback (docs/storage.md, "Rollback"):
      docker stop --time 60 $CONTAINER
      ${MIGRATE[*]} rollback
      cp -p "$FILES.before-nvme" "$FILES"
      cd "$DIR" && docker compose -p "$PROJECT" -f "$FILES" up -d --no-build --pull never --wait $SERVICE
EOF
}
trap after_stop_failure EXIT

step "1. Checks"
[ "$(id -u)" = 0 ] || fail "run as root (sudo)"
for tool in rsync findmnt docker python3 curl; do
    command -v "$tool" >/dev/null || fail "$tool is missing"
done
docker compose version >/dev/null 2>&1 || fail "docker compose plugin is missing"
python3 -c 'import yaml' 2>/dev/null || fail "PyYAML is missing: apt install python3-yaml"
mount_uuid=$(findmnt -no UUID --target "$(dirname "$(dirname "$TARGET")")" || true)
[ "$mount_uuid" = "$EXPECT_UUID" ] || fail "$(dirname "$(dirname "$TARGET")") is on UUID '${mount_uuid:-?}', expected $EXPECT_UUID - is the NVMe mounted?"
docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "container $CONTAINER not found"
label() { docker inspect -f "{{index .Config.Labels \"com.docker.compose.$1\"}}" "$CONTAINER"; }
DIR=$(label project.working_dir)
FILES=$(label project.config_files)
PROJECT=$(label project)
SERVICE=$(label service)
case "$FILES" in *,*|"") fail "container uses '$FILES' - exactly one Compose file is supported";; esac
[ -f "$FILES" ] || fail "Compose file $FILES not found"
echo "Container $CONTAINER: project $PROJECT, service $SERVICE, file $FILES"
current=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$CONTAINER")
echo "/data currently from: $current"
preflight=$("${MIGRATE[@]}" preflight)
echo "$preflight"
# finalize refuses while containers can write the data; apart from the
# service itself (stopped in step 4) none may do so - checked before the stop.
blockers=$(python3 -c '
import json, sys
users = json.loads(sys.argv[1]).get("containers")
print("unknown: %s" % users if isinstance(users, str) else " ".join(u for u in users if u != sys.argv[2]))' \
    "$preflight" "$CONTAINER")
[ -z "$blockers" ] || fail "other containers mount the data ($blockers) - the final sync would refuse after the stop; stop or reconfigure them first"

# A frame storage location outside /data (UI "Storage location") is checked
# by the guard as well: same disk = nothing to do, another disk = its UUID
# is passed on, the SD card = stop (that would keep writing frames to it).
FRAMES_ARGS=()
frames_dir=$(CONFIG="$SOURCE/config.yaml" python3 -c '
import os, yaml
try:
    config = yaml.safe_load(open(os.environ["CONFIG"])) or {}
except FileNotFoundError:
    config = {}
print(((config.get("storage") or {}).get("frames_dir") or "").strip())')
case "$frames_dir" in
    ""|/data|/data/*) echo "Frames: inside /data (moves with it)";;
    /mnt/*)
        [ -d "$frames_dir" ] || fail "frame location $frames_dir (config.yaml) does not exist on the host"
        frames_uuid=$(findmnt -no UUID --target "$frames_dir")
        root_uuid=$(findmnt -no UUID --target /)
        if [ "$frames_uuid" = "$root_uuid" ]; then
            fail "frame location $frames_dir is on the system disk (SD card) - choose a location on the NVMe in the UI first"
        elif [ "$frames_uuid" != "$EXPECT_UUID" ]; then
            echo "Frames: $frames_dir on another disk (UUID $frames_uuid) - allowed explicitly"
            FRAMES_ARGS=(--frames-uuid "$frames_uuid")
        else
            echo "Frames: $frames_dir on the NVMe"
        fi;;
    *) fail "frame location $frames_dir is neither below /data nor /mnt - check it in the UI first";;
esac

if [ "$RUN" != 1 ]; then
    echo
    echo "Checks passed. Nothing changed. Run again with --run to switch."
    exit 0
fi

if [ "$current" = "$TARGET" ]; then
    echo "The service already uses $TARGET - copy/finalize skipped."
else
    step "2. Copy while the service runs (repeatable)"
    ionice -c3 nice -n 19 "${MIGRATE[@]}" copy
    ionice -c3 nice -n 19 "${MIGRATE[@]}" copy   # second pass: keeps the final sync short

    step "3. Host Compose file -> $TARGET (validated before the stop)"
    "${MIGRATE[@]}" "${FRAMES_ARGS[@]}" --service "$SERVICE" apply-compose "$FILES"
    if ! (cd "$DIR" && "${MIGRATE[@]}" --service "$SERVICE" check-compose "$FILES"); then
        [ -f "$FILES.before-nvme" ] && cp -p "$FILES.before-nvme" "$FILES"
        fail "Compose check failed - original Compose file restored, service untouched"
    fi

    step "4. Stop $CONTAINER, final sync and verification"
    STOPPED=1
    docker stop --time 60 "$CONTAINER"
    "${MIGRATE[@]}" finalize
fi

step "5. Start from the NVMe and verify"
STOPPED=1
(cd "$DIR" && docker compose -p "$PROJECT" -f "$FILES" up -d --no-build --pull never --wait --wait-timeout 300 "$SERVICE")
mounted=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$CONTAINER")
[ "$mounted" = "$TARGET" ] || fail "/data comes from '$mounted', expected $TARGET"
health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CONTAINER")
case "$health" in healthy|running) ;; *) fail "container state: $health";; esac
port=$(docker port "$CONTAINER" 6060/tcp 2>/dev/null | head -n1 | sed 's/.*://')
base="http://127.0.0.1:${port:-6060}"
for _ in $(seq 1 30); do curl -fsS "$base/healthz" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "$base/healthz" >/dev/null || fail "$base/healthz does not answer"
diag=$(curl -fsS "$base/api/diagnostics") || fail "/api/diagnostics does not answer"
DIAG="$diag" TARGET_UUID="$EXPECT_UUID" python3 - <<'PY' || fail "storage check inside the container failed"
import json, os
storage = json.loads(os.environ["DIAG"])["storage"]
wanted = os.environ["TARGET_UUID"]
paths = storage.get("paths") or []
print("storage:", json.dumps({"paused": storage["paused"], "problem": storage["problem"],
      "paths": [{k: p.get(k) for k in ("path", "fs_uuid", "free_mb")} for p in paths]}))
assert not storage["paused"], storage["problem"]
assert any(p.get("path") == "/data" and (p.get("fs_uuid") or "").lower() == wanted for p in paths), \
    "/data is not reported on the expected UUID"
PY
"${MIGRATE[@]}" status
[ -e "$SOURCE/.ha_satimage-migrated" ] || fail "marker missing in $SOURCE"
STOPPED=0
echo "Switch verified: $CONTAINER runs with /data on $TARGET (UUID $EXPECT_UUID)."

if [ "${SKIP_SYSTEMD:-0}" = 1 ]; then
    echo "SKIP_SYSTEMD=1: systemd unit not installed."
    exit 0
fi
step "6. systemd unit $UNIT (start after a late NVMe mount)"
sed -e "s#/mnt/data2/ha_satimage/data#$TARGET#" \
    -e "s#^ExecStart=.*#ExecStart=/usr/bin/docker start $CONTAINER#" \
    "$REPO/docker/systemd/$UNIT" > "$SYSTEMD_DIR/.$UNIT.part"
mv "$SYSTEMD_DIR/.$UNIT.part" "$SYSTEMD_DIR/$UNIT"
systemctl daemon-reload
systemd-analyze verify "$SYSTEMD_DIR/$UNIT" 2>&1 | grep -v '^$' || true
systemctl enable "$UNIT"
# The container already runs: starting the unit is a no-op "docker start".
systemctl start "$UNIT"
systemctl is-enabled "$UNIT" >/dev/null && systemctl show -p Result --value "$UNIT" | grep -qx success \
    || fail "systemd unit $UNIT is not enabled/successful (the switch itself is complete)"
echo "systemd unit $UNIT enabled."
echo
echo "Done. Next: after measurement (docs/storage.md, 'Measurements'):"
echo "  bash $HERE/ha_measure.sh $CONTAINER 1800"
