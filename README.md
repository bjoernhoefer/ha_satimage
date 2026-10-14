# ha_satimage

Containerised service that downloads EUMETSAT satellite imagery, renders it
into regional crops and exposes it to **Home Assistant** as camera entities
(still image, history, MJPEG loop, GIF/MP4 animation).

> **Status:** Version 1.3.0. `msg_seviri` delivers
> real images from MSG SEVIRI (Rapid Scan every 5 minutes or 0°), `mtg_fci`
> from MTG FCI (1 km, with a raw-data archive for rendering after the fact),
> both rendered with Satpy; `data_tailor` still delivers placeholder images
> (see [CHANGELOG.md](CHANGELOG.md), [HISTORY.md](HISTORY.md) and
> [AGENTS.md](AGENTS.md)).

## Quick start

```bash
git clone https://github.com/bjoernhoefer/ha_satimage.git
cd ha_satimage
cp .env.example .env   # optional: EUMETSAT credentials, data directory
docker compose up -d --build
```

Then:

- Web UI: http://\<host\>:6060/
- Status API: http://\<host\>:6060/api/status
- Version: http://\<host\>:6060/api/version
- Example camera (Vienna): http://\<host\>:6060/regions/wien/mjpeg
- Live view in the browser (also on phones): http://\<host\>:6060/live/wien

The EUMETSAT credentials (consumer key + secret) can either be entered in
the web UI (persisted to `<data>/config.yaml`) or set via environment
variables (`EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET`, see
`.env.example`). Environment variables take precedence.

## Deployment with the prebuilt image

The target host needs no source code — only `docker-compose.yml` (and
optionally `.env`). The image is published publicly to
`ghcr.io/bjoernhoefer/ha_satimage`, so no registry login is required:

```bash
mkdir -p ~/ha_satimage && cd ~/ha_satimage
curl -sO https://raw.githubusercontent.com/bjoernhoefer/ha_satimage/main/docker-compose.yml
curl -s -o .env https://raw.githubusercontent.com/bjoernhoefer/ha_satimage/main/.env.example
docker compose up -d
```

When deploying without source code, remove the `build:` block from
`docker-compose.yml` (or set `pull_policy: always`) so Compose does not try
to build locally.

### `.env` variables for Compose

| Variable | Default | Purpose |
|---|---|---|
| `HOST_DATA_DIR` | `./data` | Host directory mounted to `/data` |
| `HA_SATIMAGE_TAG` | `latest` | Image tag (pin e.g. `1.0` to stay on a minor line) |
| `HA_SATIMAGE_PORT` | `6060` | Published web port |
| `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET` | | Optional credentials (override the UI) |

### Configuration persistence

All state (configuration, logs, product cache, source sync results and — by
default — the rendered frames) lives in a **host directory** that is
bind-mounted to `/data` in the container. Its path is set via
`HOST_DATA_DIR` in `.env` (default: `./data` next to
`docker-compose.yml`). The configuration is stored in `<data>/config.yaml`.

Because the container itself is stateless, the configuration survives image
updates, `docker compose pull` / `up -d` and container recreation.
**Backup** = copy that directory (e.g. `tar czf ha_satimage-backup.tgz data/`).

### Automatic updates (GitHub Actions)

The rollout is **tests (including UI click tests) → multi-arch image in
GHCR → deploy job on a self-hosted runner → health check / rollback**.
`.github/workflows/build.yml` deploys only pushes to `main` and manual
`workflow_dispatch` runs on `main`, never pull requests or release tags.
The runner must have labels `self-hosted`, `linux`, `ARM64` and
`deploy-satellite`. Fork PRs require manual approval before their workflows
run; PR builds stay on GitHub-hosted runners.

Host setup is separate from this repository: the runner runs as the
dedicated user `gh-deploy`, without Docker access, and may only invoke
`/usr/local/bin/ha-deploy` via sudo as `bjoern`. The deploy job does not
check out the repository or execute third-party actions on the host.
Before deployment it checks the current `main` SHA and skips superseded
builds. Deployments are serialized and use the validated, immutable image
digest from the build job.

The host deployment stack identifier remains `ha_satellite`, as registered
in the separately installed `ha-deploy` helper; it is not the image or
Compose service name. The workflow retains the `deploy-ha_satellite`
concurrency group and passes this stack identifier with the legacy
`ghcr.io/bjoernhoefer/ha_satellite` image digest required by the helper's
image allowlist. The build publishes the same image and digest under both
`ha_satimage` and `ha_satellite`; normal pulls continue to use `ha_satimage`.
Changing the workflow does not update the separately managed host Compose
files or `.env` settings.

The host-installed `ha-deploy` script pulls that digest, tags it locally
as `:latest` and runs `docker compose up -d --no-build --pull never --wait`
for the host-configured service only. It holds a host-wide `flock` lock; a
failed health check rolls back to the previous image and exits nonzero.
The host Compose image must match the helper's locally tagged
`ghcr.io/bjoernhoefer/ha_satellite:latest`; the repository Compose file
continues to use `ha_satimage` for manual installations. Compose explicitly sets
`com.centurylinklabs.watchtower.enable=false`, including protection from a
host-wide Watchtower instance.

Manual updates remain available from the installation directory:

```bash
docker compose pull && docker compose up -d
```

### Raspberry Pi resource protection and recovery

The image limits OpenMP/BLAS numerical libraries to one thread and FFmpeg
encoding/filtering to one thread. Satellite rendering, archive overlay updates
and uncached GIF/MP4 generation (including HTTP requests) share one reentrant
memory-work lock. Cached animations are served without waiting for that lock.
The scheduler reserves two download workers and one media worker, separate
from preview/archive workers, so waiting media work cannot occupy download
capacity. Downloads can still overlap a render.

Repository Compose defaults are 768 MiB RAM, 1.5 CPUs, no additional swap,
and 128 PIDs **including threads**. Override them with
`HA_SATIMAGE_MEM_LIMIT`, `HA_SATIMAGE_CPUS`, `HA_SATIMAGE_PIDS_LIMIT` in the
host `.env`. The PID ceiling is an initial safety bound, not a measured Pi
peak; verify it with your enabled sources and history settings. A limit
prevents host exhaustion but can cause an encoder/render to fail if too low.
One very large GIF can still exceed the RAM budget; reduce history/image
size or use MP4 if necessary.

**Image updates do not update the host Compose configuration.** For the
managed deployment, retain its `ha_satellite` service/image name and mounts.
Add these keys to that service in its actual host Compose file:

```yaml
mem_limit: ${HA_SATIMAGE_MEM_LIMIT:-768m}
memswap_limit: ${HA_SATIMAGE_MEM_LIMIT:-768m}
cpus: ${HA_SATIMAGE_CPUS:-1.5}
pids_limit: ${HA_SATIMAGE_PIDS_LIMIT:-128}
```

#### Pi-side setup/check script (manual administrator access)

The CD job **does run on the Raspberry Pi**, using the
`[self-hosted, linux, ARM64, deploy-satellite]` runner. However, that runner
cannot access Docker or change boot/Compose files: it may only invoke the
host-installed `ha-deploy` helper. The helper performs the rollout and health
check, not host resource configuration. Do not give the runner unrestricted
sudo/Docker access to work around this separation.

For one-time host preparation, copy the reviewed
`scripts/pi_setup.py` from this PR to the Pi and run it as an administrator.
It requires Python 3 and PyYAML (`sudo apt install python3-yaml` on Raspberry
Pi OS if missing); it does not require installing the satellite application.
For example, from your computer, after saving the script locally:

```bash
scp ./pi_setup.py USER@PI:/tmp/ha-pi-setup.py
ssh -t USER@PI 'sudo docker inspect -f "{{index .Config.Labels \"com.docker.compose.project.config_files\"}}" ha_satellite'
```

Use the **absolute host Compose path** from that output below; do not use the
cloud agent's repository path. The script supports a single Compose file and
service, and refuses a detected multi-file stack; configure such stacks
manually. Pause competing automatic deployments before applying or rolling
out. The default is check-only; it does not pull images, change files or
start containers:

```bash
ssh -t USER@PI 'sudo python3 /tmp/ha-pi-setup.py --compose-file /absolute/host/path/docker-compose.yml --service ha_satellite'
```

An exit code of 1 means a prerequisite/limit is missing; read the `STOP`
message. Unsupported memory/swap/cgroup settings must be fixed using the
OS-specific instructions below. The script **never edits boot settings or
reboots** and currently requires cgroup v2 at `/sys/fs/cgroup`.

Persist the four limits to the actual CD Compose file (confirmation required):

```bash
ssh -t USER@PI 'sudo python3 /tmp/ha-pi-setup.py --compose-file /absolute/host/path/docker-compose.yml --service ha_satellite --apply'
```

It validates effective Compose configuration before replacing anything,
rejects unrelated effective changes, preserves file ownership/permissions
and saves a private `docker-compose.yml.backup-…` alongside the original.
YAML comments/formatting are rewritten; environment expressions, mounts,
image tags and unrelated effective settings are preserved. Matching
`deploy.resources.limits` entries are updated too. Backups may contain
credentials: keep them private and never commit them. Repeated application
with the same settings does not write another backup.

Once host preparation succeeds, merge the PR with green checks; the normal
`main` pipeline tests, builds/publishes **1.7.2** and deploys via the host
helper. Do not simultaneously run manual deployment. If CD cannot deploy,
wait for the image publication and run this single SSH command instead:

```bash
ssh -t USER@PI 'sudo python3 /tmp/ha-pi-setup.py --compose-file /absolute/host/path/docker-compose.yml --service ha_satellite --apply --rollout --expected-version 1.7.2 --yes'
```

This pulls the configured image, verifies version and actual kernel limits
in an isolated probe **before** stopping the service, then recreates it with
the existing mounts/settings, waits for health and checks its actual cgroup
limits. If verification after startup fails, it stops the service rather
than leaving an unsafe workload running; it does not automatically roll
back to an older unprotected image. It never changes the selected image tag:
the host must already select `ha_satellite:latest` (or a published 1.7.2
tag); an old image is refused. For manual repository installations use
`--service ha_satimage`. `--yes` bypasses confirmation, not safety checks.

After CD, repeat the check-only command to verify actual enforcement. A
stopped service is reported as awaiting runtime verification, not as proof
of enforcement. Use the measurements below over multiple scan cycles;
successful setup/health checks alone do not establish real Pi peak usage.

#### Host memory-cgroup recovery

If Docker says "Your kernel does not support memory limit capabilities …
Limitation discarded", the configured RAM limit **does not protect the host**.
Check over SSH:

```bash
sudo docker info
cat /proc/cgroups
cat /proc/cmdline
findmnt -t cgroup,cgroup2
```

On Raspberry Pi OS, older/v1 setups can need `cgroup_enable=memory
cgroup_memory=1` on the **existing single line** in the active boot command
line file. Identify your OS/kernel and active boot file first; it is commonly
`/boot/firmware/cmdline.txt` on newer Raspberry Pi OS or `/boot/cmdline.txt` on
older installations. Back up the active file, use `sudoedit` to append only
missing parameters without removing existing boot/root settings, and reboot
in a maintenance window. Do not apply this blindly to other distributions:
a kernel without memory-controller support needs a supported kernel, and an
unmounted/misconfigured cgroup hierarchy needs an OS-specific correction.

After reboot, repeat `sudo docker info`. Do not resume heavy work while
memory-limit warnings persist. Requested values from `docker inspect` alone
are not proof of enforcement. After creating the container, verify its
**actual** memory cgroup:

```bash
CONTAINER=ha_satellite   # use ha_satimage for the repository Compose install
PID=$(sudo docker inspect -f '{{.State.Pid}}' "$CONTAINER")
cat "/proc/$PID/cgroup"
findmnt -t cgroup,cgroup2
```

For cgroup v2, use the `0::` path in that output under the cgroup2 mount
(normally `/sys/fs/cgroup`) and read `memory.max`, `memory.swap.max`,
`pids.max` and `cpu.max`. Defaults should show `805306368`, `0`, `128`,
and a CPU quota/period ratio of `1.5`, respectively. On v1 use the memory
controller path/mount and `memory.limit_in_bytes` (and, if supported,
`memory.memsw.limit_in_bytes`); PID/CPU limits are in their respective
controllers. An unlimited value means the protection is not active.
If Docker warns that swap accounting is unsupported, the no-extra-swap
setting cannot be relied on either.

#### Stop, pull and recreate over SSH

The managed host uses `ha_satellite`; manual repository installs use
`ha_satimage`. Capture the existing Compose location before stopping:

```bash
CONTAINER=ha_satellite
DIR=$(sudo docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$CONTAINER")
SERVICE=$(sudo docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$CONTAINER")
sudo docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$CONTAINER"
sudo docker stop --time 30 "$CONTAINER"
# Only if graceful stopping fails:
# sudo docker kill "$CONTAINER"
cd "$DIR"
```

Apply the limits above to the host Compose service, then check
`sudo docker compose config`. If the deployment uses non-default/multiple
Compose files, supply the same `-f` files reported by `config_files` to
**all** Compose commands below. Ensure the selected satellite image uses
`:latest` rather than a pinned version/digest, and pause any competing
automatic deployment during recovery.

```bash
sudo docker compose config --images
sudo docker compose pull "$SERVICE" &&
sudo docker compose up -d --no-build --pull never \
  --force-recreate --wait --wait-timeout 180 "$SERVICE"
sudo docker compose ps "$SERVICE"
sudo docker compose logs --tail 100 "$SERVICE"
```

For a private image, first run `sudo docker login ghcr.io -u <username>` and
enter a `read:packages` token at the password prompt. Do not put the token in
a command. Bind-mounted configuration/history is preserved; do not use
`down -v`, remove data directories, or kill unrelated Python processes.
Pulling `latest` before a new build is published can retrieve the same image.

#### Measure the Pi under real load

After verifying cgroup enforcement, monitor while preview renders, archive
backfill and desktop/phone GIF/MP4 requests run:

```bash
sudo docker stats "$CONTAINER"
# In another SSH terminal:
sudo docker top "$CONTAINER" -eo pid,ppid,nlwp,pcpu,pmem,args
free -h
vmstat 1
curl --max-time 5 -o /dev/null -sS -w 'health: %{http_code}, %{time_total}s\n' \
  http://127.0.0.1:6060/healthz
```

On cgroup v2 also read `memory.peak` and `pids.peak` if your kernel exposes
them, `memory.events` (particularly `oom`/`oom_kill`), `pids.current`,
`pids.events`, and `cpu.stat` from the same cgroup directory. Record peak
RAM/thread counts and health/UI responsiveness over several scan cycles.
Allow PID headroom for HTTP traffic, render children, FFmpeg and healthchecks;
investigate rising counts rather than simply raising the cap. Reduce enabled
sources, archive work or history if the measured workload does not fit.
These host measurements cannot be substituted by tests on a CI runner.

When migrating, stop and remove any previously installed
`ha_satimage_watchtower` container. GitHub Actions still publishes images
for release tags; tag builds do not trigger deployment.

### Versioning and releases

ha_satimage uses [semantic versioning](https://semver.org/). The version is
defined in `src/ha_satimage/__init__.py` (`__version__`) and in
`pyproject.toml` (both must match; current: **1.3.0**). It is shown in the
web UI footer and returned by `GET /api/version`.

Releases are git tags `vX.Y.Z`; CI builds them into the image tags `X.Y.Z`,
`X.Y` and `latest`. Select the tag with `HA_SATIMAGE_TAG` in `.env`
(default `latest`; pin e.g. `1.0` to stay on a minor line). Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## Features

- **Source catalogue**: the available sources are configurable in the UI
  (table with enable switches + collapsible JSON editor). Drivers:
  `msg_seviri` (Rapid Scan Europe from the EUMETSAT Data Store + local Satpy
  rendering, default; one download per scan shared by all regions, ~100 MB,
  i.e. ~9 GB/day at a 15 min interval, ~28 GB/day at 5 min), `data_tailor`
  (server-side cropping at EUMETSAT), `mtg_fci` (MTG FCI, ~1 km: only the
  European chunks, ~180 MB per scan). An automatic sync with the EUMETSAT
  Data Store checks availability/freshness and suggests new collections.
- **Scheduled maintenance**: the EUMETSAT
  [RSS schedule](https://user.eumetsat.int/resources/service-statuses/rss-schedule)
  is checked regularly. During a maintenance window Rapid Scan sources are not
  queried and their regions show a black "Currently unavailable due to
  maintenance" image with the start and end of the window. Once per hour
  the source is still checked; if it delivers again, normal images resume
  early.
- **Regions**: name, centre (lat/lon), radius in km (the bounding box is
  derived from it), output size, composite, source. Preconfigured examples:
  `wien` (Vienna) and `mallorca` with the composite
  `natural_color_hrv_with_night_ir` (true colour by day, sharpened to ~1 km
  with the HRV channel, IR clouds at night; more in AGENTS.md).
- **Locations on a map of Europe** (up to 4): "+ Add location" and
  "📍 Change location" open a zoomable map of Europe (land, sea and country
  borders in the same style as the image overlay). Click/tap marks the
  centre, the radius is set with a slider; latitude/longitude can also be
  typed in directly (plain WGS84 – no Meteosat pixel or line numbers). The
  other locations are shown on the map. A current satellite image of Europe
  can be loaded as map background on demand ("🛰 Load current satellite
  map", rendered from the latest MSG SEVIRI scan). Moving or removing a
  location resets its image history.
- **FCI raw-data archive**: while an `mtg_fci` source is active, the raw data
  (Europe + all regions) is kept for 12 h (configurable). With "Download all
  sources" every region is rendered in every image type right after each
  download; the "🗄 Archive" viewer shows only these ready-made images
  (nothing is rendered while browsing).
- **Border overlay**: country/administrative borders and worldwide coastlines
  (including islands). Enabling borders or upgrading the overlay updates
  historical archive PNGs and JPEGs without deleting their scans. On startup
  with "Download all sources", missing FCI images are rebuilt from retained raw data. Already deleted scans
  without retained raw data cannot be recovered. Moving a location or disabling
  baked-in borders still invalidates its rendered archive.
- **History**: rolling ring buffer per region under
  `<storage location>/{region}/`, duration configurable in the UI (default
  60 minutes), plus a storage limit (MB) as an emergency brake. Old frames
  and orphans are cleaned up automatically.
- **Storage location** of the frames selectable in the UI (e.g.
  `/mnt/data/ha_satimage`; `docker-compose.yml` mounts `/mnt` for this),
  including moving existing frames and a warning for the system disk or
  non-persistent paths.
- **Web API** (details in [AGENTS.md](AGENTS.md)):
  `GET/POST /api/config`, `GET /api/status`, `GET /api/version`,
  `POST /api/regions`, `PUT/DELETE /api/regions/{region}` (locations),
  `GET /api/location-map`,
  `GET /healthz`, `GET /regions/{region}/latest.png` (or `latest.jpg`),
  `GET /regions/{region}/frames/{i}.png`,
  `GET /regions/{region}/animation.gif`, `GET /regions/{region}/animation.mp4`,
  `GET /regions/{region}/mjpeg`.
- **Web UI** at `/` (phone-friendly): previews and history are clickable →
  fullscreen viewer (arrow keys/swipe, timelapse), live stream in the
  browser, sources, storage location, credentials (secret masked), cycle
  per source, status, **live logs** at the bottom and the version in the
  footer.

## Home Assistant integration

Ready-to-copy YAML snippets are in [docs/homeassistant.md](docs/homeassistant.md).

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,ui]"
playwright install chromium   # for the UI click tests
pytest -q
uvicorn ha_satimage.main:app --reload --port 6060
```

Architecture, deployment and operating rules: [AGENTS.md](AGENTS.md).
Chronicle and pitfalls: [HISTORY.md](HISTORY.md).
Release notes: [CHANGELOG.md](CHANGELOG.md).
