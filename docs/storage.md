# Storage on the data disk, migration and measurements

This document covers running ha_satimage on a host whose system disk
should be spared, by example of the Raspberry Pi **nzbpi**:

| Item | Value |
|---|---|
| System + Docker data directory | SD card `/dev/mmcblk0p2` |
| Data disk | NVMe at `/mnt/data2`, filesystem UUID `e16ccc22-790c-4f89-97f5-b7caca060bbd` |
| Old data directory | `/home/bjoern/ha_satellite/data` (SD card) |
| New data directory | `/mnt/data2/ha_satimage/data` → `/data` |
| Host Compose service / container | `ha_satellite` (kept, see below) |

## What lands where

Inside the container everything persistent and every larger temporary
file lives under `/data` (configuration, `source_sync.json`,
`maintenance.json`, `logs/`, product cache `cache/` incl. the downloaded
`.nat` files, `frames/`, `_archive/`, `_renders/`, `location_map/`, Satpy
data `satpy/`, XDG cache `cache/xdg/`, temporary files `tmp/` incl. MP4
encoding). Python bytecode is compiled into the image, so imports do not
write to the container layer.

Remaining accesses to Docker's data directory (on nzbpi: the SD card) that
this repository cannot move:

- **Image layers** are read when Python modules are imported. The render
  child processes now start from a pre-loaded *forkserver* instead of a
  fresh interpreter (`spawn`): previously each render re-read ~200 MiB of
  module files, which matches the measured ~174 MiB/10 s of SD reads once
  the page cache was evicted under memory pressure.
- **Docker's stdout log** (`json-file`): size-limited to 2 × 2 MB, the image
  only logs warnings there (full log: `/data/logs/ha_satimage.log`,
  1 MB × 4) and uvicorn's access log is off. Both levels can be changed at
  runtime in the web UI ("Logs": record level and container output; defaults
  `HA_SATIMAGE_LOG_LEVEL=INFO`, `HA_SATIMAGE_STDOUT_LOG_LEVEL=WARNING`).
  Keep the container output at `WARNING` or higher on the Pi; use `DEBUG`
  only temporarily for the record level (it goes to the NVMe).
- **Container state** writes by Docker for every healthcheck (every 2 min).
- Moving Docker's data directory (`data-root`) to the NVMe is a host
  decision outside this repository.

## Protection against a missing data disk

Three independent layers, none of them only in CI:

1. **Compose** binds with `create_host_path: false`: if the path does not
   exist (disk not mounted, directory never created), Docker refuses to
   start the container instead of creating an empty directory on the SD
   card. Note: Docker then does **not** retry at boot; see the optional
   systemd unit below.
2. **Entrypoint** (every container start incl. restart after reboot):
   `python -m ha_satimage.storage_guard` checks, before anything is created
   or chowned, that `/data` (and an external frame location from
   `config.yaml`) is on a filesystem with `HA_SATIMAGE_REQUIRE_FS_UUID`
   (resolved via the read-only host `/dev/disk/by-uuid` bind and
   `/proc/self/mountinfo`), not on the container's own filesystem, has at
   least `HA_SATIMAGE_MIN_FREE_MB` free and is writable for the service user.
   Otherwise it exits with code 78 and a message such as
   `/data is on /dev/mmcblk0p2 (179:2), not on UUID e16c… - is the disk mounted on the host?`.
   A directory carrying the marker `.ha_satimage-migrated` (old location
   after migration) is rejected as well.
3. **App**: the same check at startup (exit code 78), every 5 minutes
   (downloads and renders pause while it fails, e.g. disk full, and resume
   automatically), and before a frame storage location is changed in the
   UI. A missing `TMPDIR` never falls back to `/tmp`. An exclusive lock
   `/data/.instance.lock` refuses a second instance on the same data.

Other hosts set `HA_SATIMAGE_REQUIRE_FS_UUID` to their UUID
(`findmnt -no UUID <dir>`), or explicitly to `none`. The image itself has
no UUID default (only the repository Compose file has the nzbpi UUID), so
other deployments with their own Compose file are unaffected.

`/mnt` stays bind-mounted for frame storage locations chosen in the UI,
with Docker's default propagation (`rprivate`): a disk unmounted on the
host while the container runs stays visible inside instead of exposing the
empty mount point directory. A frame location on a *different* disk than
`/data` needs `HA_SATIMAGE_FRAMES_FS_UUID` (or `none`).

### Optional: start after a late mount

`docker/systemd/ha-satimage-after-mount.service` starts the container once
`/mnt/data2/ha_satimage/data` is mounted (`RequiresMountsFor`). It does not
change `docker.service` or other containers. Install only after review (see
the comments in the file).

## Migration (nzbpi)

Tool: `scripts/ha_storage_migrate.py` (Python 3 standard library, `rsync`,
`findmnt`, `docker`; run as root on the host). Every command can be
repeated; nothing in the source is ever deleted; the target is only
written if it is empty or was created by the tool
(`/mnt/data2/ha_satimage/.ha_satimage-migration.json`); `finalize` and
`rollback` refuse while a container mounts source or target (the tool never
stops containers).

**Approvals needed** (in this order): (A) merge/deploy of this version,
(B) creating the directory on the NVMe and copying while the service runs,
(C) a maintenance window: stopping `ha_satellite`, final sync, editing the
host Compose file, starting again.

### 0. Deploy the new version on the old path (approval A)

The new image runs with the unchanged host Compose file (the UUID check is
off there, `/data` is still a bind mount). It already brings the forkserver,
bounded resampling, backoff and log limits. **Check first** that the SD card
has more than 1 GiB free (`df -h /home/bjoern/ha_satellite/data`), otherwise
lower `HA_SATIMAGE_MIN_FREE_MB` for this step. Take the
[before measurement](#measurements) before deploying.

### Recommended: one controlled run (steps 1 + 2 + systemd unit)

`scripts/ha_nzbpi_switch.sh` performs steps 1 and 2 below in the safe
order and verifies the result; on success it installs the systemd unit
(set `SKIP_SYSTEMD=1` to skip). It never deletes anything and never rolls
back by itself: if a step after the stop fails, it stops and prints the
state and the manual rollback commands.

```bash
ssh nzbpi
git clone --depth 1 https://github.com/bjoernhoefer/ha_satimage.git /tmp/ha_satimage
sudo bash /tmp/ha_satimage/scripts/ha_nzbpi_switch.sh          # checks only
sudo bash /tmp/ha_satimage/scripts/ha_nzbpi_switch.sh --run    # switch
```

Order: checks (root, tools, NVMe UUID, container labels → Compose file,
project and service, preflight, frame location) → `copy` twice while the
service runs → `apply-compose` (host Compose file switched to the NVMe,
backup `*.before-nvme`, comments are not kept) + `check-compose`, both
**before** the stop (on a failed check the original file is restored) →
`docker stop` → `finalize` → `docker compose -p <project> up` of the same
service → verified: `/data` mount source, health, `/healthz`, storage
state in `/api/diagnostics` (UUID of `/data`, not paused), marker in the
old directory → systemd unit. A frame storage location outside `/data`
is checked first: on the NVMe nothing to do, on another disk its UUID is
set as `HA_SATIMAGE_FRAMES_FS_UUID`, on the SD card the script stops
(choose another location in the UI first). Every step can be repeated by
running the script again.

Do not push to `main` while it runs (CD would recreate the container).

The manual steps follow.

### 1. Preparation (approval B, service keeps running)

```bash
scp scripts/ha_storage_migrate.py nzbpi:/tmp/
ssh nzbpi
findmnt -no SOURCE,UUID,TARGET /mnt/data2      # must show e16ccc22-…
sudo python3 /tmp/ha_storage_migrate.py preflight
sudo ionice -c3 nice python3 /tmp/ha_storage_migrate.py copy   # repeatable
```

`preflight` checks root, rsync, the source, the target's filesystem UUID
(nothing is created if the NVMe is not mounted), free space (source size −
already copied + 10 % + 2 GiB reserve), a foreign non-empty target and the
containers using the paths. Only mounts of the source/target itself or a
path below it count (plus any overlapping mount of another
ha_satellite/ha_satimage instance); broad parent mounts of other containers
such as `/` (node_exporter) or `/mnt/data2` (filebrowser, radarr, sabnzbd)
are only listed as `parent_mounts` and do not block. `ha_nzbpi_switch.sh`
stops before touching the service if anything else would block.
The dry run also validates the switched Compose file (`preview-compose`, fed
to `docker compose config` via stdin; the host file is not written).
`docker compose config` omits `create_host_path: false` (`"bind": {}`), so
the check reads the explicit `false` from the file and treats a missing key
in the effective config as false. `copy` uses `rsync -aHAX --numeric-ids`
(owners, permissions, times, hard links, ACLs, xattrs; without
`.instance.lock`, `tmp/`, `*.part`) at idle I/O priority. Repeat it shortly
before the maintenance window to keep the final sync short.

### 2. Switch-over (approval C, maintenance window)

```bash
# Prevent CD from recreating the container meanwhile:
#   GitHub → Settings → Variables → DEPLOY_TARGETS = azure (or none)
DIR=$(sudo docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' ha_satellite)
FILES=$(sudo docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' ha_satellite)
sudo docker stop --time 60 ha_satellite
sudo python3 /tmp/ha_storage_migrate.py finalize      # sync with --delete, verify, marker
sudo cp -p "$FILES" "$FILES.before-nvme"              # backup of the host Compose file
sudo python3 /tmp/ha_storage_migrate.py apply-compose "$FILES"   # or sudoedit, see below
sudo python3 /tmp/ha_storage_migrate.py check-compose "$FILES"
cd "$DIR" && sudo docker compose -f "$FILES" up -d --no-build --pull never --wait ha_satellite
sudo docker logs ha_satellite 2>&1 | grep 'storage check'   # "... UUID e16ccc22-…"
curl -s http://127.0.0.1:6060/api/diagnostics | python3 -m json.tool | head -40
# Afterwards restore DEPLOY_TARGETS.
```

`finalize` verifies with an rsync dry run (no differences in content,
permissions, owners, links) plus SHA-256 of `config.yaml` and all `*.json`
files (`--full-checksum` compares every file, i.e. reads all data once
more). Only then it writes `.ha_satimage-migrated` into the old directory;
the new image refuses to start on it, so an outdated Compose file cannot
silently write there again. If `finalize` is interrupted, run it again.

Host Compose changes for the `ha_satellite` service (keep the service name,
image, port and the other settings; the name must stay so `ha-deploy` and
the concurrency group keep replacing the *same* container — a renamed
second stack would run in parallel):

```yaml
    volumes:
      - type: bind
        source: /mnt/data2/ha_satimage/data
        target: /data
        bind:
          create_host_path: false
      - type: bind
        source: /mnt
        target: /mnt
        bind:
          create_host_path: false
      - type: bind
        source: /dev/disk/by-uuid
        target: /run/host-disk-by-uuid
        read_only: true
        bind:
          create_host_path: false
    environment:
      # ... keep the existing entries, add:
      - HA_SATIMAGE_REQUIRE_FS_UUID=e16ccc22-790c-4f89-97f5-b7caca060bbd
    logging:
      driver: json-file
      options:
        max-size: "2m"
        max-file: "2"
```

These edits live in the host file and are therefore kept by every later
CD deployment (`ha-deploy` only replaces the image). The observed memory
limit of 1,700,003,840 bytes differs from the repository default (768 MiB);
leave it unchanged for the switch-over so the measurements stay
comparable, and lower it only after the after measurement.

### Rollback

- **Before `finalize`:** nothing was switched; the service runs as before.
  The partial copy on the NVMe can stay or be removed manually later.
- **After the switch-over** (also days later; data written on the NVMe
  meanwhile is kept):

  ```bash
  # DEPLOY_TARGETS = azure/none during the rollback
  sudo docker stop --time 60 ha_satellite
  sudo python3 /tmp/ha_storage_migrate.py rollback   # NVMe → old dir, without --delete, removes marker
  sudo cp -p "$FILES.before-nvme" "$FILES"
  cd "$DIR" && sudo docker compose -f "$FILES" up -d --no-build --pull never --wait ha_satellite
  ```

  `rollback` copies everything new or changed on the NVMe back without
  deleting anything; files that were removed on the NVMe meanwhile (e.g.
  frames rotated out of the ring buffer) come back in the old directory and
  are cleaned up by the app's orphan cleanup / retention. The NVMe copy is
  kept. A later new attempt starts again with `copy`.
- **Image:** if the new version itself misbehaves, revert the merge on
  `main` (CD deploys the previous code). An older image has no storage
  guard; it works with both directories.

Nothing deletes the old directory. Remove it manually only after a
successful longer run on the NVMe and a backup.

## Memory and job protection (summary)

- Render windows much finer than the output (e.g. the Europe overview
  from the 1 km HRV grid: 12.4 Mpx for a 1.8 Mpx image, synthetic peak
  ~1.3 GiB = the observed OOM size) are block-averaged before resampling to
  at most `HA_SATIMAGE_MAX_SOURCE_OVERSAMPLING` (default 4) source pixels per
  output pixel (synthetic: ~630 MiB).
- One render child at a time (global lock), with an RSS watchdog: budget
  `HA_SATIMAGE_RENDER_MEMORY_MB`, default container limit − 192 MiB. Time
  limit 300 s. Exceeding either, an OOM kill (`memory.events`) or a
  `MemoryError` are *resource errors*: no immediate retry; the combination
  (region, source, image type) pauses 15 min, doubling up to 6 h; "Refresh
  now" or a configuration change ends the pause. Other crashes are retried
  once after 10 s.
- Output is only accepted as a complete PNG (signature + IEND); frames,
  indexes and state files are written atomically (`.part` + rename). A
  broken ring buffer index is rebuilt from the files instead of deleting
  them as orphans.
- Download errors back off exponentially (5, 10, 20, 40, 60 min).
- At most one pending render per region; archive runs are limited to one
  queued run per source.
- `GET /api/diagnostics`: render runs/failures by cause, maximum
  parallelism, peak RSS, budget, render queue, backoff, storage state and
  download errors. Each render child logs outcome, duration and peak RSS.

## Measurements

Run on the host, the same way before (step 0) and after the switch-over,
each over the same duration during normal operation (not after a restart,
which fills caches), ideally several times at comparable times of day:

```bash
scp scripts/ha_measure.sh nzbpi:/tmp/
ssh nzbpi 'sudo bash /tmp/ha_measure.sh ha_satellite 1800'
```

The script is read-only and samples only at start and end (plus `vmstat`).
It reports host-wide and container (`io.stat`) bytes read/written per
device (SD = `179:x`, NVMe = `259:x`), the container memory peak and OOM
kills, I/O wait average/maximum, kernel `Card stuck being busy` messages
and the app's render statistics. Expected after the switch: container
writes on 179:x close to zero, SD reads only after page-cache eviction,
no OOM kills, no render failures by `oom_killed`/`memory_budget` (or, if
they occur, paused combinations instead of repeats).

A temporarily low load average proves nothing: compare SD bytes, iowait,
OOM counters and the kernel messages over several cycles. These measures
reduce the application's load on the SD card; they cannot rule out or fix
a defective card.
