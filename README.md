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
concurrency group and passes this stack identifier with the renamed
`ghcr.io/bjoernhoefer/ha_satimage` image digest. Host-side image allowlists,
Compose service names and `.env` settings must support the renamed image
and service; changing the workflow does not update that host configuration.

The host-installed `ha-deploy` script pulls that digest, tags it locally
as `:latest` and runs `docker compose up -d --no-build --pull never --wait`
for `ha_satimage` only. It holds a host-wide `flock` lock; a failed health
check rolls back to the previous image and exits nonzero. The CD host must
use `HA_SATIMAGE_TAG=latest` (the default). Compose explicitly sets
`com.centurylinklabs.watchtower.enable=false`, including protection from a
host-wide Watchtower instance.

Manual updates remain available from the installation directory:

```bash
docker compose pull && docker compose up -d
```

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
  (Europe + all regions) is kept for 12 h (configurable). Via the
  "🛰 FCI archive" button any region — including newly created ones — can
  be rendered at any archived time in any image type.
- **Border overlay**: country/administrative borders and worldwide coastlines
  (including islands). Enabling borders or upgrading the overlay updates
  historical archive PNGs and JPEGs without deleting their scans. On startup,
  missing FCI images are rebuilt from retained raw data. Already deleted scans
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
