#!/usr/bin/env bash
# Read-only, low-overhead before/after measurement of ha_satimage on a host.
#
#   sudo scripts/ha_measure.sh [CONTAINER] [SECONDS]     (defaults: ha_satellite 600)
#
# Samples once at the start and once at the end (plus vmstat every 10 s), so
# it adds practically no I/O itself. Reports per block device (SD card =
# major 179, NVMe = major 259):
#   - host-wide bytes read/written (/proc/diskstats),
#   - bytes read/written by the container's cgroup (io.stat),
#   - memory peak and OOM kills of the container (memory.peak/.events),
#   - I/O wait (vmstat "wa"), kernel "Card stuck being busy" messages,
#   - the app's own render statistics (/api/diagnostics).
# Writes nothing except to stdout. Run it with the same duration before and
# after a change and under comparable conditions (same sources, scan times).
set -euo pipefail

CONTAINER="${1:-ha_satellite}"
SECONDS_TOTAL="${2:-600}"
PORT="${HA_SATIMAGE_PORT:-6060}"

DISKSTATS="${DISKSTATS:-/proc/diskstats}"   # overridable for tests

cgroup_dir() {
    [ -n "${CGROUP_DIR:-}" ] && { echo "$CGROUP_DIR"; return; }
    local pid rel
    pid="$(docker inspect -f '{{.State.Pid}}' "$CONTAINER")"
    rel="$(awk -F: '$1=="0"{print $3}' "/proc/$pid/cgroup")"
    echo "/sys/fs/cgroup$rel"
}

disk_snapshot() {  # name read_bytes write_bytes for SD/NVMe devices
    # %.0f, not %d: mawk clips %d at 2^31, which made every difference 0.
    awk '$1==179 || $1==259 {printf "%s %.0f %.0f\n", $3, $6*512, $10*512}' "$DISKSTATS"
}

io_snapshot() {  # major:minor rbytes wbytes
    awk '{r=0; w=0; for (i=2;i<=NF;i++){split($i,kv,"="); if(kv[1]=="rbytes")r=kv[2]; if(kv[1]=="wbytes")w=kv[2]} print $1, r, w}' \
        "$1/io.stat" 2>/dev/null || true
}

CG="$(cgroup_dir)"
PEAK_FILE="$(mktemp)"
trap 'rm -f "$PEAK_FILE"' EXIT
echo "Container $CONTAINER, cgroup $CG, sampling ${SECONDS_TOTAL}s"
disk_before="$(disk_snapshot)"
io_before="$(io_snapshot "$CG")"
oom_before="$(awk '$1=="oom_kill"{print $2}' "$CG/memory.events" 2>/dev/null || echo '?')"
since="$(date '+%Y-%m-%d %H:%M:%S')"

# Only numeric sample lines (vmstat may repeat its header); the first sample
# is the average since boot and is skipped. "wa" is located by its header.
vmstat -n 10 $(( SECONDS_TOTAL / 10 + 1 )) | awk '
    !col && /^ *r +b/ {for (i=1;i<=NF;i++) if ($i=="wa") col=i; next}
    col && $1 ~ /^[0-9]+$/ {if (++seen==1) next; v=$col+0; wa+=v; n++; if (v>max) max=v}
    END {if (n) printf "iowait: avg %.1f%%, max %d%% (vmstat, %d samples of 10 s)\n", wa/n, max, n; else print "iowait: no samples"}' &
VMSTAT=$!
# Memory peak of this interval (memory.peak counts since container start);
# reads cgroupfs only, no disk I/O.
( peak=0; end=$(( $(date +%s) + SECONDS_TOTAL ))
  while [ "$(date +%s)" -lt "$end" ]; do
      cur=$(cat "$CG/memory.current" 2>/dev/null || echo 0)
      [ "$cur" -gt "$peak" ] && peak=$cur
      sleep 2
  done
  echo "$peak" > "$PEAK_FILE" ) &
SAMPLER=$!
sleep "$SECONDS_TOTAL"
wait "$VMSTAT" || true
wait "$SAMPLER" || true

echo "Host block devices (MiB read / written in the interval):"
join <(echo "$disk_before" | sort) <(disk_snapshot | sort) |
    awk '{printf "  %-12s read %8.1f  written %8.1f\n", $1, ($4-$2)/1048576, ($5-$3)/1048576}'
echo "Container cgroup io.stat (MiB read / written; 179:x = SD, 259:x = NVMe):"
join <(echo "$io_before" | sort) <(io_snapshot "$CG" | sort) |
    awk '{printf "  %-8s read %8.1f  written %8.1f\n", $1, ($4-$2)/1048576, ($5-$3)/1048576}'
echo "Memory: peak in the interval $(awk '{printf "%.0f MiB", $1/1048576}' "$PEAK_FILE" 2>/dev/null || echo '?') (sampled every 2 s)"
echo "Memory: peak $(awk '{printf "%.0f MiB", $1/1048576}' "$CG/memory.peak" 2>/dev/null || echo '?') (since container start), limit $(cat "$CG/memory.max" 2>/dev/null || echo '?')"
echo "OOM kills in the interval: before $oom_before, after $(awk '$1=="oom_kill"{print $2}' "$CG/memory.events" 2>/dev/null || echo '?')"
echo "Kernel 'Card stuck being busy' messages: $(journalctl -k --since "$since" 2>/dev/null | grep -c 'Card stuck being busy' || true)"
echo "Render statistics (/api/diagnostics):"
diag="$(curl -fsS --max-time 5 "http://127.0.0.1:$PORT/api/diagnostics" 2>/dev/null || true)"
printf '%s' "$diag" |
    python3 -c 'import json,sys; d=json.load(sys.stdin); r=d["render"]; print("  runs %s ok %s failures %s max parallel %s max peak RSS %s MiB budget %s MiB; backoff %d, storage paused %s" % (r["runs"], r["succeeded"], r["failures"], r["max_parallel"], r["max_peak_rss_mb"], r["memory_budget_mb"], len(d["backoff"]), d["storage"]["paused"]))' 2>/dev/null ||
    echo "  (not available - version without /api/diagnostics)"
