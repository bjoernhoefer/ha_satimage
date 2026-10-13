# AGENTS.md — Operating manual for ha_satimage

> **MANDATORY for EVERY change (code, template, config, Docker):**
> Before committing, **run the web UI click tests and make them pass**
> (`pytest -q`, including `tests/test_ui.py` with Playwright — not skipped!).
> New or changed UI features get **their own click test in the same
> commit** (desktop, and for display/interaction topics additionally the
> phone view via the `mobile_page` fixture). Details under [Tests](#tests).

> **MANDATORY before creating a PR:** propose the next version number
> (semver: patch = fix, minor = feature, major = breaking change), bump it
> in **both** `src/ha_satimage/__init__.py` (`__version__`) and
> `pyproject.toml`, add a `CHANGELOG.md` entry, and verify with
> `pytest tests/test_version.py` that both versions match.

## Target environment

| Item | Value |
|---|---|
| Host | Any Docker host, `linux/amd64` or `linux/arm64` (e.g. Raspberry Pi 5) |
| Operation | Docker Compose (`docker-compose.yml`), GitHub Actions for auto updates |
| Consumers | One or more Home Assistant instances (see `docs/homeassistant.md`) |
| Network | Private network; remote sites via any VPN/routing that reaches `<host>:6060` |
| Auth | None — private network only |
| Web port | `6060` |
| State | Host directory bind-mounted to `/data` (`HOST_DATA_DIR`, default `./data`) |

Integration happens **in each Home Assistant instance** (see
`docs/homeassistant.md`).

## Architecture

```
src/ha_satimage/
  __init__.py     __version__ (semantic version, must match pyproject.toml)
  config.py       Configuration model (Pydantic) + YAML persistence + env override,
                   source catalogue, storage location, history
  geometry.py      Bounding box computation from centre + radius
  overlay.py       Country border overlay (Natural Earth 1:10m, overlay_data/),
                   for region squares (LAEA) and the lon/lat map of Europe
  location_change.py  Locations (max. 4): map of Europe (outline + on-demand
                   satellite background), add/move/remove, history reset
  buffer.py        Ring buffer (frames per region, cleanup, storage limit, move)
  maintenance.py   EUMETSAT RSS maintenance schedule (page parser, cache in
                   maintenance.json) + black "unavailable" maintenance image
  status.py        Status store (last/next run, errors, frame count)
  scheduler.py     APScheduler jobs (non-blocking): download per source at its
                   scan cycle, render after a new scan, single render lock,
                   source sync job
  media.py         Cached thumbnails/JPEGs and animations (GIF/MP4)
  sources/         Drivers: msg_seviri, data_tailor, mtg_fci (+ dummy)
    eumetsat.py      Data Store search + download (eumdac), product cache /data/cache
    fci_archive.py   MTG FCI raw-data archive: chunk geometry, download per slot, cleanup
    satpy_render.py  Satpy rendering in a child process (crop window, resampling, PNG)
    hrv_composite.py Custom compositor: true colour sharpened with HRV
  satpy_config/    Custom Satpy composite definitions (composites/seviri.yaml, visir.yaml)
  source_sync.py   Sync of the catalogue with the EUMETSAT Data Store (public API)
  storage.py       Storage location candidates, write test, system disk/container warning
  logbuffer.py     In-memory log (for the UI) + rotating log file
  main.py          FastAPI app: web UI + REST API + image endpoints
  templates/       Jinja2 template of the web UI (incl. fullscreen viewer)
```

State lives under `/data` (configuration `config.yaml`, `source_sync.json`,
`logs/`, product cache `cache/`) and at the configured frame storage
location (default `/data/frames`, changeable in the UI, e.g.
`/mnt/data/ha_satimage`; the FCI archive lives there under `_archive/`).
`/data` is a host bind mount (`HOST_DATA_DIR`), so the container
itself is stateless and can be recreated/updated at any time. Region names
must not start with `_` (reserved for internal directories).

### Source catalogue

`sources.catalog` in `config.yaml` defines which sources are available
(`id`, `driver`, `label`, `collection`, `enabled`, `description`).
Optional `cycle_minutes` (scan cycle, see "Download at the scan cycle").
Several entries may use the same driver (e.g. `msg_seviri` for 0°, Rapid
Scan, IODC). Regions reference an `id` via `source`; only regions with an
enabled source are fetched automatically. In the UI the catalogue is
editable as a table (enable switch) and as collapsible JSON. The
`collection` of an `msg_seviri` entry determines which Data Store collection
is loaded (default entry `msg_seviri` = Rapid Scan
`EO:EUM:DAT:MSG:MSG15-RSS`). Old configurations with `sources.active: [...]`
or `sources.msg_collection` are migrated automatically on load.

The **source sync** (`source_sync.py`, button "Sync now" or automatically
every `sources.auto_sync_hours` hours, 0 = off) queries the public
browse/search endpoints of the EUMETSAT Data Store: whether the collection
exists, time of the latest product, and newly available collections
supported by a driver ("discovered", adoptable with one click — they are
added disabled). Result in `/data/source_sync.json`, separate from the
configuration.

### Scheduled maintenance (maintenance.py)

EUMETSAT announces planned Rapid Scan Service interruptions on
`https://user.eumetsat.int/resources/service-statuses/rss-schedule`
(env override `HA_SATIMAGE_MAINTENANCE_URL`, used by the tests). The page is
fetched lazily — only when a `msg_seviri` source with an RSS collection
(`AFFECTED_COLLECTIONS`, e.g. `EO:EUM:DAT:MSG:MSG15-RSS`) is downloaded or
rendered — at most every `sources.maintenance_check_hours` hours (default 6,
0 = off; after a fetch error every 30 min), and cached in
`<data>/maintenance.json`. The parser is deliberately tolerant (common date
notations, consecutive start/end pairs, max. 14 days, times in UTC;
publication/update dates are ignored).

During an active window the download job does **not** query the Data Store
(no error, no warning; INFO log once per window) and re-checks at the cycle.
Once per hour (`MAINTENANCE_PROBE_INTERVAL`, first one hour after the window
started) it still queries the source to verify it is down; failures or no
scan newer than the window start/last scan only log at INFO. A newer scan
ends the window early for that source (`DownloadState.maintenance_override`):
the probe's result is processed like a normal download and regions render
real images again.
Every region of the source gets one black frame "Currently unavailable due to
maintenance" + "From <start> to <end> UTC" (composite `maintenance`, so the
first real scan afterwards renders immediately). Active/next window per
source: `GET /api/sources` → `downloads[<id>].maintenance`, shown in the
source table; the raw page result is `GET /api/sources` → `maintenance`.

### Phase model

- **Phase 1 (done):** skeleton — configuration, web server, API, ring
  buffer, scheduler; all endpoints work end-to-end but deliver generated
  placeholder images ("dummy frames"). This allowed verifying the ARM64
  build and the whole infrastructure early, before the considerably more
  complex Satpy/eumdac chain.
- **Phase 2 (done):** `msg_seviri` delivers real images: the newest product
  of the configured collection (default `EO:EUM:DAT:MSG:MSG15-RSS`, Rapid
  Scan Europe, every 5 min) is loaded via `eumdac` (only the `.nat` file,
  ~100 MB), stored **once per product** in `/data/cache/` and used by all
  regions. Rendering uses Satpy (`seviri_l1b_native`) in a **child process**
  (memory is fully released afterwards; an OOM does not hit the web server).
  Frame timestamp = end of scan. `data_tailor` still delivers dummy frames.
- **Phase 3 (current):** `mtg_fci` delivers real images from MTG FCI (1 km
  visible, 2 km IR) via a raw-data archive, see below.
- `Source.render(region, config, last_sensing) -> RenderedFrame`: if the
  newest scan is not newer than `last_sensing` (newest frame in the buffer),
  `NoNewData` is raised — no error, no duplicate frame.

### Composites (msg_seviri)

Default `natural_color_hrv_with_night_ir` (true colour by day, sharpened
with the ~1 km HRV channel; IR clouds at night). Custom composites live in
`src/ha_satimage/satpy_config/composites/seviri.yaml` (compositor in
`sources/hrv_composite.py`) and are included in the child process via
`satpy.config config_path`. Other working composites: `natural_color_hrv`
(day only), `natural_color_raw_with_night_ir` (previous default, ~3 km),
`natural_color` (black at night), `convection`, `airmass`, `cloudtop`,
`colorized_ir_clouds`, `hrv_clouds`. Do **not** use
`natural_color_with_night_ir` (downloads NASA BlackMarble at runtime, fails
on the hash). Unknown names lead to an error in the status, not a crash.
Composites from channels of different resolution are only created during
resampling: each channel is cropped with its own window, loading uses
`generate=False` (see HISTORY.md, "HRV sharpening"). Works for Rapid Scan
and 0° (full disk). Reference values on a Raspberry Pi 5: ~8–9 s and
~400–450 MB peak RSS per region and run.

### Archive of all sources ("download all sources")

`archive.render_all` (UI: "Download all sources") makes **every** enabled
source download at its own cycle — even when no region uses it — and, after
each new scan, renders **every region in every image type** of that source's
driver. The images are stored by `archive.py` as PNG **and** JPEG under
`<storage location>/_renders/<region>/<source>/<composite>/<scan>.png`.
After both files are written the image is added to the region's list of
available images `_renders/<region>/available.json` (also updated by
pruning and overlay updates; rebuilt once from disk if missing). The archive
viewer reads **only** this list — no file lookups, and **nothing is ever
rendered on request** (an image that is not listed returns 404). Cleanup by age (`archive.render_retention_hours`, default 24 h) and
by a storage limit (`archive.render_max_storage_mb`, default 2000 MB, oldest
first); regions that no longer exist are dropped, and a region whose cut-out
changed (position, radius, size — see `region_signature`) loses its
archived images because they no longer match. Enabling borders or upgrading
the overlay updates existing images in place, retaining their scan timestamps.

Rendering runs strictly serialized under the **global render lock**, one
image at a time (memory!), in the scheduler jobs `archive-<source>`. These
run in their own single-thread APScheduler executor (`archive`), at most one
queued run per source (further new scans are covered by it), and yield the
render lock to waiting preview renders after every image — so the preview
of the regions never falls behind a long archive run; an
immediate run over all sources is `POST /api/archive/render` (button
"Archive all now"). Combinations that already exist are skipped, so a
restart or a second run costs nothing. For `mtg_fci` an archive run covers
**every** raw slot in the FCI archive (within `render_retention_hours`,
newest first, each rendered from its own slot), not only the newest one —
slots missed earlier (run lagging behind, lock busy, restart, slots older
than enabling "Download all sources") are backfilled; only one such run per
source at a time, it re-reads the slot list after every image.

The per-region button "🗄 Archive" opens the archive viewer with a source
and an image type select, JPEG/PNG format selection and time-lapse. Only
images from the list of available images are shown (raw FCI scans without
rendered images are not offered). Without "Download all sources" nothing is
archived.

### Country border overlay

Per region, `borders: true` (default, checkbox "Draw country and regional
borders") makes the render child process draw worldwide national borders
and coastlines including islands (black, ~1 px) and worldwide administrative borders (states, provinces and
similar regions; black, fainter, ~0.75 px) as anti-aliased hairlines into the
image (`overlay.py`, 4× supersampled), for SEVIRI and FCI (also archive
renders; the cache name contains the switch). Data: Natural Earth 1:10m
`admin_0_boundary_lines_land` + `admin_1_states_provinces_lines` + `coastline` (public
domain), packed in `overlay_data/borders_10m.json.gz` (~4.1 MB, regenerate
with `scripts/build_borders.py`). Projected into the same LAEA as the target
region — no pycoast/GSHHS needed. Toggling counts like a
composite change (the frame remembers `borders`) and re-renders immediately.
Enabling borders updates existing archive PNGs and JPEGs without needing the
original raw data; overlay upgrades add only missing layers. Disabling baked-in
borders invalidates the archive and (with `render_all`) regenerates from
available source data. At startup with `render_all` on, retained FCI raw
slots without images are rendered. Already deleted images without retained raw data cannot be recovered.

### MTG FCI and raw-data archive (mtg_fci)

An FCI slot (every 10 min, collection `EO:EUM:DAT:0662`) is ~1 GB, split
into 40 strips ("chunks", 1 = south … 40 = north). Only chunks
`archive.chunk_min`..`archive.chunk_max` (default 32–40 = Europe,
~180 MB/slot, ~25 s) are downloaded, **plus** any chunk a configured region
needs (`fci_archive.chunks_for_region`: bounding box → geos projection →
row in the 2 km grid; tested against pyproj). Storage:
`<storage location>/_archive/<collection>/<slot>/` (`.part` + rename,
`meta.json` last), retention `archive.retention_hours` (default 12 h,
0 = newest slot only; ~13 GB at a 10 min cycle). Both in the UI under
"Storage location & history".

- Downloading starts as soon as an `mtg_fci` catalogue entry is **enabled**
  (own download job `download-<id>` at the 10 min cycle, without the render
  lock — I/O only), regardless of whether a region uses the source.
- Rendering only uses the region's chunks: keeps
  the child process at ~410–480 MB. With all 9 chunks the peak was
  ~600–640 MB and it was occasionally killed next to the web server.
- There is **no on-demand rendering**: archive images of FCI slots are only
  created by the archive run after a download ("Download all sources").
- Reader `fci_l1c_nc` needs `netCDF4` (wheel available for aarch64) and runs
  with the **synchronous** dask scheduler: HDF5 from the wheel is not
  thread-safe; with threads there were SIGSEGVs in ~50 % of runs.
- Composites (UI list per driver, `config.FCI_COMPOSITES`): default
  `natural_color_with_night_cloudtop` (custom definition in
  `satpy_config/composites/visir.yaml`, ~11 s / ~410 MB), `natural_color`,
  `hrv_clouds`, `cloudtop`, `colorized_ir_clouds`. **Not**: `true_color*`
  (> 6 GB, time limit) and `airmass` (~1.7 GB). SEVIRI names are mapped to
  an FCI counterpart (`FCI_COMPOSITE_ALIASES`); the UI picks a matching
  image type when the source changes.

### Locations and the map of Europe (location_change)

A location is a region: centre in plain WGS84 lat/lon + radius in km (no
satellite pixel/line numbers). At most `MAX_LOCATIONS` = **4** — enforced
by `POST /api/regions` and by every configuration save
(`location_change.check_locations`; an existing configuration with more
regions stays loadable but cannot grow). New names: `[A-Za-z0-9][A-Za-z0-9_-]{0,31}`;
radius 25–2000 km. A new location takes the source of the first existing
one and that driver's default image type.

The editor (button "+ Add location" / "📍 Change location", deep link
`#location=<name|new>`) shows a zoomable map of Europe
(`EUROPE_EXTENT` = 25° W–45° E, 33–72° N, 1400×1281 px). It is a plain
lon/lat grid (plate carrée, pixel aspect for ~52° N), so pixel ↔ lat/lon is
linear in the browser; the marked square is `geometry.bounding_box`.
Backgrounds:

- **outline** (`/location-map/outline.png`, generated, always available):
  Natural Earth 1:50m land (`overlay_data/europe_land_50m.json.gz`, ~33 KB,
  regenerate with `scripts/build_europe_land.py`) + country borders and
  Austrian states via `overlay.draw_borders_lonlat` (same style as
  `draw_borders`, 2× supersampling for memory).
- **satellite** (`POST /api/location-map/satellite`, button "🛰 Load current
  satellite map"): rendered on demand under the global render lock from the
  newest MSG SEVIRI product (downloaded if not cached; source =
  `location_change.map_source`) with composite
  `natural_color_raw_with_night_ir` (~3 km — HRV for all of Europe would
  exceed the memory limit) via `RenderRequest.lonlat_extent`; stored as
  JPEG in `<data>/location_map/` until rendered again.

Moving (lat/lon/radius/size changed) or removing a location drops its ring
buffer (`BufferManager.drop`, under the render lock) — the history must not
mix cut-outs, and the new cut-out renders immediately instead of waiting
for the next scan (`NoNewData` would otherwise apply).

## ARM64 build decision

ARM64 hosts (e.g. Raspberry Pi 5) are a primary target. Satpy's
dependencies `pyresample`, `pykdtree` and `pyproj` build very slowly from
the PyPI sdist on ARM (native extensions, PROJ C library) and need extra
system headers. `mambaorg/micromamba` with the `conda-forge` channel offers
ready `linux-aarch64` builds for exactly these packages, so
`docker/Dockerfile` is based on micromamba. **As of phase 2:** conda-forge
has no current `trollimage` for linux-aarch64 (Satpy could not be solved),
while PyPI now has manylinux-aarch64 wheels for `trollimage`, `pyresample`,
`pykdtree`, `pyproj` and `numpy`. conda therefore only provides Python; the
Satpy chain comes via `pip install --only-binary=…` (no sdist build; if a
wheel is missing, the build fails). Build on a Pi 5: ~3 min, image ~1.1 GB.
Alternatives (e.g. `python:3.12-slim` + `apt` headers + pip, or prebuilt
wheels from piwheels) were rejected: they meant either very long build
times on ARM itself (no cross-compile in CI) or extra maintenance from
manual wheel pinning.

## Deployment

```bash
cd <install-dir>       # directory containing docker-compose.yml and .env
docker compose pull    # or --build when building locally
docker compose up -d
```

- **Configuration persistence:** all state lives in a host directory
  bind-mounted to `/data`, path set via `HOST_DATA_DIR` in `.env`
  (default `./data` next to `docker-compose.yml`). The configuration is
  `<data>/config.yaml`. It survives image updates and container
  recreation; backup = copy that directory. `docker/entrypoint.sh` aligns
  the ownership of `/data` (and the selected frame storage location) with
  the internal user on start (see HISTORY.md, "Bind mount ownership").
- **Frame storage location:** Compose mounts the host's `/mnt` at `/mnt`;
  in the web UI ("Storage location & history") choose e.g.
  `/mnt/data/ha_satimage`, existing frames are moved on request. The
  service runs as `PUID`/`PGID` (default 1000) — the target directory must
  be writable for it, e.g. once
  `sudo install -d -o 1000 -g 1000 /mnt/data/ha_satimage`. The UI warns if
  a path is on the same disk as `/data` (system disk) or only in the
  container filesystem (not persistent).
- **Image:** a multi-arch image (`linux/amd64`, `linux/arm64`) is built by
  `.github/workflows/build.yml` and pushed to
  `ghcr.io/bjoernhoefer/ha_satimage` on every push to `main` and on tags.
  If the package is private, the deployment user needs `docker login ghcr.io`.
- **Auto updates:** tests → GHCR image → deploy job on a self-hosted ARM64
  runner labelled `deploy-satellite` → host-installed `ha-deploy` with a
  health check and rollback on failure (nonzero exit). Only pushes and manual
  workflow runs on `main` deploy; stale builds are skipped. The runner runs
  as dedicated user `gh-deploy`, without Docker access, and may only call
  `/usr/local/bin/ha-deploy` via sudo as `bjoern`. It checks out no repository
  code and runs no third-party actions. Host setup is separate; `ha-deploy`
  uses a host-wide `flock` lock and tags the build digest locally as `latest`
  before `docker compose up -d --no-build --pull never --wait` for this
  service. The CD host uses `HA_SATIMAGE_TAG=latest`. Fork PR workflows
  require manual approval and never deploy. Watchtower is explicitly disabled
  by the container label. Manual updates remain
  `docker compose pull && docker compose up -d`.
- **Compose `.env` variables:** `HOST_DATA_DIR` (host data directory,
  default `./data`), `HA_SATIMAGE_TAG` (image tag, default `latest`; pin
  e.g. `1.0` to stay on a minor line), `HA_SATIMAGE_PORT` (default `6060`),
  plus the optional EUMETSAT credentials. `HA_SATIMAGE_DATA_DIR` is an
  in-container app variable, not the host path.
- Memory limit in Compose (`deploy.resources.limits.memory: 768M`), so the
  service coexists with other workloads on small hosts.

### Versioning and releases

- Semantic version in `src/ha_satimage/__init__.py` (`__version__`) and
  `pyproject.toml`; both must match (`tests/test_version.py`). Current:
  **1.7.0**.
- Shown in the web UI footer and returned by `GET /api/version`.
- Releases are git tags `vX.Y.Z`; CI builds them into the image tags
  `X.Y.Z`, `X.Y` and `latest`.
- Every release gets an entry in `CHANGELOG.md`.
- Before creating a PR: propose the next version (patch = fix, minor =
  feature, major = breaking), bump it in both files and run
  `pytest tests/test_version.py` (see the mandatory rule at the top).

### Download at the scan cycle

Each source has a cycle (`SourcesConfig.cycle_for`): `cycle_minutes` of the
catalogue entry, otherwise the known value of the collection
(`config.COLLECTION_CYCLES`: Rapid Scan 5, 0°/IODC 15, FCI 10 min) or of the
driver, otherwise `sources.poll_interval_minutes`. It also determines the
number of frames in the ring buffer (history / cycle).

- For each active source with a download driver (`msg_seviri` only if a
  region uses it; `mtg_fci` always, for the archive) a cycle job
  `download-<id>` runs (every 20 s, only queries when due). Due: end of scan
  + cycle + observed delivery delay; not yet available → every minute, more
  than one cycle overdue → at the cycle, error → after ≤ 5 min.
- Downloads do not hold the render lock. A **new** scan triggers rendering
  of all regions of the source; rendering uses the locally stored product
  (no second Data Store access).
- Placeholder sources (`dummy`, `data_tailor`) render at a fixed interval.
- Cycle, last scan, next download and errors per source:
  `GET /api/sources` (`downloads`), in the UI in the source table.

### Media cache

Thumbnails (`?w=240`), JPEGs for the MJPEG stream and GIF/MP4 live under
`<storage location>/<region>/_cache/` (`media.py`) and are pre-generated
after every new frame in the job `media-<region>` (outside the render
lock). Animations carry a hash of the frame list and frame rate in their
name; the ring buffer cleans up thumbnails of removed frames. The GIF is
only pre-generated up to 60 frames (PIL keeps all frames in memory), beyond
that only on request; MP4 reads the frames one by one.

The UI uses these formats: preview per region = `latest.jpg`; the viewer has
a format selection (`viewer-format`, remembered in the browser via
`localStorage`, in the hash as `f=`): history JPEG (default, fast) or PNG
(original); live MJPEG stream, MP4 video (`<video>`, zoomable) or GIF. The
archive offers JPEG and PNG as well.

## Operating rules

- **One render at a time**, system-wide (not just per region) — via
  `threading.Lock` in `RenderScheduler`. Simultaneously due runs wait for
  the lock (max. 15 min) and use the same, once-loaded product.
- Download/rendering run via APScheduler in their own thread pool and do
  not block the asyncio web server.
- Errors (missing slots, timeouts, invalid credentials) are logged and
  recorded in the `StatusStore` (`GET /api/status`) but do **not** crash
  the service; the next scheduled run is the retry.
- The ring buffer index (`_index.json`) per region records the order;
  frame index `0` = newest frame. On start the disk is reconciled with the
  index: missing files are removed from the index, files without an index
  entry ("orphans") are deleted.
- Configuration changes via `POST /api/config` immediately re-create the
  scheduler jobs (`scheduler.reload()`). Only new or changed regions (e.g.
  different source/composite) render immediately; unchanged ones wait for
  the next scan or keep their interval.
- Every frame records its source (`source`) and composite in the index.
  "No new scan" (`NoNewData`) only applies if the newest frame has the same
  origin — after a switch it renders immediately, even if the new source
  has an older scan (0° lags Rapid Scan by ~15 min). Buffer order is the
  order of insertion, not of scan time.
- A storage location change holds the render lock
  (`scheduler.exclusive()`), checks the target path with a write test and
  merges the ring buffer indexes when moving.
- **Logs** are central: render start/end, skipped runs, errors,
  configuration and storage changes, source sync are logged at
  INFO/WARNING. The UI shows them live at the bottom (`GET /api/logs`),
  additionally rotating in `/data/logs/ha_satimage.log`. New features must
  log their relevant steps as well.
- **No secrets in the repo.** `.env.example` documents the available
  variables; real values only in `.env` (gitignored) or in the web UI
  (stored in `<data>/config.yaml`).

## API overview

| Method/path | Purpose |
|---|---|
| `GET /` | Configuration UI (bottom: API link collection with region selection; footer: version) |
| `GET /api/config` | Current configuration (secret masked) |
| `POST /api/config` | Save configuration (merged onto stored values; empty or masked secret = unchanged) |
| `GET /api/version` | Running version (`__version__`) |
| `GET /api/status` | Last/next run, frame count, age, errors per region |
| `POST /api/regions` | Add a location (`name`, `lat`, `lon`, `radius_km`; max. 4) |
| `PUT /api/regions/{region}` | Move a location (`lat`, `lon`, `radius_km`); resets its history |
| `DELETE /api/regions/{region}` | Remove a location and its history |
| `GET /api/location-map` | Map of Europe: extent, size, limit, locations, satellite background status |
| `GET /location-map/outline.png` | Map of Europe (land, sea, borders) |
| `GET /location-map/satellite.jpg` | Satellite background (after rendering) |
| `POST /api/location-map/satellite` | Render the satellite background now (downloads the newest product if needed) |
| `POST /api/regions/{region}/refresh` | Immediate render run ("Refresh now") |
| `GET /api/regions/{region}/frames` | History: frames in the buffer (newest first) with stable URLs |
| `GET /api/sources` | Source catalogue, result of the last sync, cycle/download/maintenance status per source (`downloads`), RSS maintenance schedule (`maintenance`) |
| `POST /api/sources/sync` | Sync with the EUMETSAT Data Store now |
| `POST /api/sources/adopt` | Adopt a discovered collection (`{"collection": ...}`) into the catalogue |
| `GET /api/storage` | Current storage location, usage, candidates with free space/warnings |
| `POST /api/storage` | Set storage location/history/archive (`frames_dir`, `move_existing`, `history_minutes`, `max_storage_mb`, `archive_retention_hours`, `archive_chunk_min`, `archive_chunk_max`, `archive_render_all`, `archive_render_retention_hours`, `archive_render_max_storage_mb`) |
| `GET /api/archive` | Archive: FCI raw data (retention, chunks, slots/usage, last error) and pre-rendered images (`renders`, `render_all`, retention, limit) |
| `GET /api/regions/{region}/archive` | Archive of a region: sources, image types, images (`?source=&composite=`; `slots` = alias of `images`) |
| `GET /regions/{region}/archive/{source}/{composite}/{slot}.{png\|jpg}` | Archived image from the list of available images (never rendered on request; 404 otherwise) |
| `POST /api/archive/render` | Archive all enabled sources now (all regions, all image types) |
| `GET /api/logs` | Log entries (`?after=<id>`, `?format=text`) |
| `GET /live/{region}` | Memorable address: opens the live stream in the browser |
| `GET /healthz` | Liveness |
| `GET /regions/{region}/latest.png` | Newest frame |
| `GET /regions/{region}/latest.jpg` | Newest frame as cached JPEG (preview in the UI) |
| `GET /regions/{region}/frames/{i}.png` | Frame `i` from the buffer (`0` = newest) |
| `GET /regions/{region}/history/{file}` | Frame by file name (`<name>.png` original, `<name>.jpg` cached full-size JPEG, `?w=240` = JPEG thumbnail) |
| `GET /regions/{region}/animation.gif` | Animation of the history (GIF, cached) |
| `GET /regions/{region}/animation.mp4` | Same as MP4 (requires `imageio-ffmpeg`) |
| `GET /regions/{region}/mjpeg` | MJPEG stream, loops the frames (Home Assistant camera) |

## Tests

```bash
pip install -e ".[dev,ui]"
playwright install chromium   # once, for the UI click tests
pytest -q
```

`eumdac` and the Satpy child process are mocked in `tests/test_sources.py`;
the test servers from `tests/conftest.py` start with regions on the `dummy`
source, so no test talks to the real Data Store.

Covered: configuration handling (load/save, env override, masking, source
catalogue, migration), ring buffer logic (max frames, storage limit, orphan
cleanup, move), bounding box computation, version consistency
(`tests/test_version.py`: `__init__.py` vs. `pyproject.toml`), the APIs
against a real uvicorn process (`tests/test_api_config.py`,
`tests/test_api_features.py`, EUMETSAT via a local fake server) and
**browser click tests of the web UI** (`tests/test_ui.py`, Playwright).

**Click tests are mandatory for every change** (see the note at the top).
They secure the core features: enter/save credentials, secret stays masked
and is kept when saving without re-entry, env override locks the fields,
save interval, "Refresh now" without page change, click preview →
fullscreen viewer fills the viewport (desktop **and** phone viewport
390×844 with touch, back button closes), browse history/timelapse, pick an
image from the dropdown, zoom in the viewer (+/−/reset, keys, drag,
desktop + phone), format selection in the viewer (JPEG/PNG, live
MJPEG/MP4/GIF, desktop + phone), live stream in the browser incl. deep link
`/live/{region}`, logs at the bottom, source JSON (collapsed, editable,
validation), enable sources, sync + adopt, scheduled maintenance (badge,
maintenance image, check interval; desktop + phone), change storage location incl.
move, no horizontal scrolling on phones, change image type/source per
region (re-renders without another click), toggle country borders per
region (desktop + phone), archive viewer (source and image type select,
paging, format switch, deep link `#archive=…&s=…&c=…&f=…`, desktop +
phone), archive settings and "Archive all now", locations on the map of Europe (add by clicking on the zoomed map, enter lat/lon, change incl. history reset, remove/cancel, limit of 4, zoom/pan, satellite background error, deep link, desktop + phone), show cycle per source and change it via JSON
(desktop + phone), placeholder sources marked, API links follow the
selected region and are clickable, version shown in the footer.
Selectors use `data-testid` exclusively — markup and styling may change
freely, the IDs must be kept. Without Playwright installed the click tests
are skipped locally; in CI `HA_SATIMAGE_REQUIRE_UI_TESTS=1` enforces that
they run.
