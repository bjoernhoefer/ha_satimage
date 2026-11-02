# Changelog

All notable changes to this project are documented in this file.
The project follows [Semantic Versioning](https://semver.org/).

## [1.11.0] — 2026-11-02

### Added

- Storage guard (`ha_satimage.storage_guard`): on every container start
  (entrypoint, before anything is created or chowned), at app start, every
  5 minutes and before a storage location change, `/data` and an external
  frame location must be on the expected filesystem
  (`HA_SATIMAGE_REQUIRE_FS_UUID`, `HA_SATIMAGE_FRAMES_FS_UUID`, resolved via
  a read-only bind of the host's `/dev/disk/by-uuid`), not on the container
  filesystem, with at least `HA_SATIMAGE_MIN_FREE_MB` free and writable.
  Failure = exit code 78 with an explanation, or paused downloads/renders
  while running; no fallback to the system disk or `/tmp`. Instance lock
  `/data/.instance.lock` against two instances on the same data.
- `scripts/ha_storage_migrate.py`: repeatable, verifiable migration of the
  data directory (preflight, copy while running, finalize with the service
  stopped, verify, rollback, Compose check); never deletes the source,
  never overwrites a foreign target, marks the old directory as migrated.
- `scripts/ha_nzbpi_switch.sh`: one controlled run of the switch on nzbpi
  (checks, copy, Compose rewrite + validation before the stop, final sync,
  verified start, then the systemd unit); dry check without `--run`, no
  automatic rollback. `ha_storage_migrate.py apply-compose` rewrites the
  host Compose file idempotently with a backup.
- `scripts/ha_measure.sh`: read-only before/after measurement (SD vs. NVMe
  I/O per device, container `io.stat`, memory peak, OOM kills, iowait,
  render statistics). Optional systemd template
  `docker/systemd/ha-satimage-after-mount.service`.
- `GET /api/diagnostics`: render runs and failures by cause, parallelism,
  peak RSS, memory budget, queues, backoff, storage state, download errors.
- Documentation `docs/storage.md` (layout, guard, migration, rollback,
  measurements, remaining host dependencies).
- Log levels: `HA_SATIMAGE_LOG_LEVEL` (UI log + log file, default `INFO`)
  and `HA_SATIMAGE_STDOUT_LOG_LEVEL` (container output = Docker log on the
  system disk, default `WARNING`) are defaults; the "Logs" section of the web
  UI (config `logging.level` / `logging.stdout_level`) changes both at
  runtime. `DEBUG` only affects ha_satimage's own loggers.
- Every protection logs what it does: active limits at start-up ("Resource
  protection: ..."), coarsened windows, backoff pauses, repeated failures
  and recovery, skipped paused archive combinations, de-duplicated queue
  entries, storage problems and recovery.

### Changed

- **Compose default data directory** `HOST_DATA_DIR` is now
  `/mnt/data2/ha_satimage/data` (NVMe of the reference Raspberry Pi) with
  the matching UUID check; all binds use `create_host_path: false`. Other
  hosts must set `HOST_DATA_DIR` and `HA_SATIMAGE_REQUIRE_FS_UUID` (or
  `none`). The image has no UUID default; the managed `ha_satellite`
  deployment keeps its names (see docs/storage.md).
- Render child processes start from a pre-loaded forkserver instead of
  `spawn` (no ~200 MiB module re-read per render from the image layers).
- Render windows much finer than the output are block-averaged before
  resampling (`HA_SATIMAGE_MAX_SOURCE_OVERSAMPLING`, default 4): the Europe
  overview from HRV/1 km data peaked at ~1.3 GiB (synthetic), now ~0.6 GiB.
- RSS watchdog with a memory budget per render child
  (`HA_SATIMAGE_RENDER_MEMORY_MB`, default container limit − 192 MiB).
  Timeouts, OOM kills (`memory.events`), budget overruns and `MemoryError`
  are resource errors: no immediate retry, the combination pauses with
  exponential backoff (15 min … 6 h). Other crashes retry once after 10 s.
- Download errors back off exponentially (up to 1 h); one pending render per
  region at most.
- Temporary files (`TMPDIR=/data/tmp`, incl. MP4 encoding) on the data disk;
  bytecode compiled into the image; no `VOLUME`; recursive `chown` only when
  the top-level owner differs.
- Logs: Docker log limited to 2 × 2 MB, stdout only warnings
  (`HA_SATIMAGE_STDOUT_LOG_LEVEL`), no uvicorn access log, healthcheck every
  2 min; every render child logs outcome, duration and peak RSS.

### Fixed

- Incomplete render output (truncated PNG) is no longer stored as a frame.
- Image build: `TMPDIR=/data/tmp` is only set for the runtime (micromamba
  failed at build time because `/data` does not exist in the image).
- Archive runs pause a combination only after a resource error; a single
  broken raw slot is retried by the next run instead of pausing the image type.
- Windows below 4 Mpx are never coarsened (HRV detail of regular regions kept).
- Frames, ring buffer indexes and state files are written atomically; an
  unreadable index is rebuilt from the frame files instead of deleting all
  frames as orphans.

## [1.10.1] — 2026-11-01

### Fixed

- Media prewarm: a prewarm queued while another one for the same region was
  still running was dropped by the scheduler, leaving the newest frame
  without a pre-generated thumbnail/JPEG. One prewarm may now queue behind
  the running one.
- Storage relocation: a prewarm queued before a storage move no longer
  recreates the old region directory (`_cache`) at the previous location.
- Flaky CI tests that blocked image publication and rollout: thumbnail,
  composite-change (desktop + phone) and viewer-format reload click tests
  now wait for the asynchronous state they check (also the shared
  `ensure_frames` helper and the archive "render all" test); the unwritable-path
  check is skipped when running as root (permissions do not apply).

## [1.10.0] — 2026-10-29

### Added

- Selectable rollout targets: the Raspberry Pi (`pi`) and the Azure VM
  (`azure`, runner labels `self-hosted`, `linux`, `X64`,
  `deploy-satellite-azure`). Pushes to `main` deploy to the repository
  variable `DEPLOY_TARGETS` (`pi`, `azure`, `both`, `none`; unset = `pi`),
  manual runs choose `deploy_targets`. Every target receives the same image
  digest, deploys independently and is serialized per host.

## [1.9.0] — 2026-10-23

### Added

- Add a third location, `europe`, showing the full European overview
  (25° W–45° E, 33–72° N) with borders off by default. All source-specific
  image types, history formats and archived variants remain available.
- Add the overview once on upgrade when a location slot and an enabled
  satellite source are available. Existing locations are preserved;
  removing the overview does not recreate it on restart.
- Cover three-location layouts and overview interactions on desktop and phone.

### Fixed

- Refresh previews and history when the first completed render is observed,
  including when the page initially loaded before images were ready.

## [1.8.1] — 2026-10-16

### Changed

- Remove the redundant per-region Live and Time-lapse buttons; these views
  remain available through the viewer and archive.

## [1.8.0] — 2026-10-14

### Added

- Add a confirmed "Clear archive" action to delete archived images and FCI
  raw slots while preserving the normal region history and configuration.
- Persist a scan-time cutoff so cleared scans are not downloaded or
  backfilled again, including after restart. Newer scans continue to be
  archived normally.
- Coordinate clearing with active downloads and rendering, cancel stale
  archive work, and report deletion counts through `POST /api/archive/clear`.

## [1.7.2] — 2026-10-14

### Fixed

- Serialize GIF/MP4 generation with satellite rendering, including HTTP
  requests and archive overlays, to avoid concurrent memory-heavy jobs.
- Limit numerical-library and FFmpeg threads; reserve separate scheduler
  capacity for downloads and a single media prewarm worker.
- Add configurable container PID/thread and swap limits, and document
  Raspberry Pi memory-cgroup recovery and resource verification.
- Provide a Pi-side setup script with read-only preflight checks, private
  Compose backups, explicit resource-limit application and guarded rollout
  with actual cgroup verification.

## [1.7.1] — 2026-10-13

### Fixed

- MTG FCI High Resolution (HRFI, `EO:EUM:DAT:0665`): only image types that
  can be made from its four channels are offered and archived (IR sandwich
  with colorized IR at night as default, IR sandwich, HRV clouds, colorized IR
  clouds). Previously the archive tried natural color and cloud tops for every
  slot and logged "Composite … is not available for fci_l1c_nc" errors.

## [1.7.0] — 2026-10-13

### Changed

- All page sections of the web UI are foldable and collapsed by default, so
  the page loads smaller. Navigation links and `#sec-…` deep links open the
  matching section.

## [1.6.0] — 2026-10-13

### Changed

- The archive is purely file-based: after a new download, every region is
  rendered in every image type (PNG and JPEG) and added to the region's list
  of available images (`_renders/<region>/available.json`). The archive
  viewer only reads this list; nothing is rendered on request any more (MTG
  FCI raw scans without rendered images are no longer offered, unlisted
  images return 404).
- Without "Download all sources" nothing is rendered into the archive, also
  not at startup or after border changes (overlays of existing images are
  still updated in place).

### Fixed

- Gaps of 30 minutes and more in the region previews: archive renders now
  run in their own executor, at most one queued run per source, and yield
  the render lock to waiting preview renders after every image, so they no
  longer block or starve the regular preview renders.

## [1.5.2] — 2026-10-12

### Fixed

- GitHub Actions publishes the same image under the legacy `ha_satellite`
  name and deploys that digest to satisfy the host helper's image allowlist,
  while retaining `ha_satimage` for normal image pulls.

## [1.5.1] — 2026-10-12

### Fixed

- GitHub Actions deployment retains the host helper's `ha_satellite` stack
  identifier and concurrency group while deploying the renamed `ha_satimage`
  image, avoiding the `unknown stack 'ha_satimage'` failure.

## [1.5.0] — 2026-10-08

### Added

- Scheduled EUMETSAT maintenance of the Rapid Scan Service is honoured: the
  [RSS schedule](https://user.eumetsat.int/resources/service-statuses/rss-schedule)
  is checked every `sources.maintenance_check_hours` hours (default 6, 0 = off,
  editable in the UI). During a maintenance window RSS sources are not queried
  (no download errors/warnings in the log) and their regions show a black
  image "Currently unavailable due to maintenance" with the start and end of
  the window. Active and upcoming windows are shown in the source table and
  returned by `GET /api/sources`. Downloads resume automatically afterwards.
- During a maintenance window the source is still queried once per hour to
  verify that it is down (logged at INFO, no error). If it delivers a new
  scan before the announced end, downloads and normal images resume early.

## [1.4.0] — 2026-10-07

### Added

- Added the satellite emoji favicon to the web UI.

## [1.3.2] — 2026-10-05

### Fixed

- MTG FCI raw downloads normalize timezone-aware scan timestamps to UTC.
  Archive cache checks and writes use the raw slot directory's identity,
  including legacy slots whose metadata or Satpy timestamps differ.
- On-demand renders keep the global render lock until PNG and JPEG publication
  completes, preventing duplicate rendering during parallel requests.
- Missing archive JPEGs are regenerated from cached PNGs without rendering or
  requiring retained raw data; JPEG requests always receive JPEG content.
- Regression coverage includes timezone offsets, fractional seconds, midnight,
  legacy metadata, restart, concurrent requests and desktop/mobile browsing.

## [1.3.1] — 2026-10-05

### Fixed

- Adding country borders or upgrading the overlay no longer deletes historical
  archive images. Borders are applied to existing PNGs and their JPEGs are
  regenerated, preserving all scan timestamps even without retained raw data.
  Existing bordered images receive only the missing coastline layer.
- On startup, missing FCI archive images are backfilled from retained raw slots,
  even when "Download all sources" is off. Images already deleted by older
  versions can only be recovered while their raw data is still available.
- The border overlay now includes worldwide coastlines, including Mallorca,
  Ibiza and Menorca, on satellite images and the location map.
- Updated archive URLs invalidate browser caches after an overlay update.

## [1.3.0] — 2026-10-05

### Added

- Continuous deployment after successful tests and GHCR image publication
  on `main`, using a self-hosted ARM64 runner labelled `deploy-satellite`.
  Deployments use the exact build digest and skip superseded builds.
  The separately installed host script `ha-deploy` performs health checks,
  rollback on failure and host-wide deployment locking.

### Changed

- Replaced the optional Watchtower service with GitHub Actions deployment.
  The application now explicitly opts out of host-wide Watchtower updates.
  Manual updates with Docker Compose remain supported.

## [1.2.2] — 2026-10-03

### Fixed

- On-demand MTG FCI renders are now cached under the raw archive slot's
  sensing time, so the rendered image is found on subsequent requests instead
  of being rendered again when Satpy reports a rounded scan time.

## [1.2.1] — 2026-10-02

### Fixed

- Archive ("Download all sources"): MTG FCI images were often not
  pre-rendered, so the archive viewer had to render them from raw data on
  request ("Rendering from raw data …"). The archive run only rendered the
  newest raw slot (and, if it lagged behind, a newer slot than the one that
  triggered it), so missed slots were never rendered. It now renders every
  archived FCI raw slot that still lacks images (newest first, within the
  render retention, each from its own slot) and backfills slots downloaded
  before the option was enabled or while the render lock was busy.

## [1.2.0] — 2026-10-01

### Added

- **Locations on a map of Europe** ("location_change"): up to 4 locations
  can be added, moved and removed in the web UI. "+ Add location" and
  "📍 Change location" open a zoomable map of Europe (wheel/pinch, drag,
  +/− buttons) with land, sea and country borders in the same style as the
  image overlay (incl. Austrian states); click/tap marks the centre, the
  radius is set with a slider, latitude/longitude can also be typed in
  (plain WGS84, no Meteosat pixel numbers). Other locations are shown on the
  map. Deep link `#location=<name>` / `#location=new`.
- On-demand satellite background for the map ("🛰 Load current satellite
  map"): rendered from the latest MSG SEVIRI scan (downloaded if needed)
  with country borders and cached until it is rendered again.
- API: `POST /api/regions`, `PUT /api/regions/{region}`,
  `DELETE /api/regions/{region}`, `GET /api/location-map`,
  `GET /location-map/outline.png`, `GET /location-map/satellite.jpg`,
  `POST /api/location-map/satellite`.
- Natural Earth 1:50m land polygons for Europe
  (`overlay_data/europe_land_50m.json.gz`, `scripts/build_europe_land.py`).
- Unit, API, rendering and click tests (desktop and phone) for locations
  and the map.

### Changed

- At most 4 locations: saving a configuration that adds a fifth region is
  rejected (existing larger configurations remain usable); duplicate region
  names are rejected.
- Moving or removing a location deletes its image history, so the history
  never mixes cut-outs and the new cut-out renders immediately.

## [1.1.0] — 2026-10-01

### Added

- **Download all sources**: a new switch "Download all sources" (under
  "Storage location & history") downloads every enabled source at its own
  cycle (MSG SEVIRI 0° every 15 min, Rapid Scan every 5 min, MTG FCI every
  10 min) and renders **every region in every image type** of that source
  right after a new scan. The images are stored as PNG and JPEG under
  `_renders/<region>/<source>/<image type>/<scan>.png` at the frame storage
  location; cleanup by age (`render_retention_hours`, default 24 h) and by
  a storage limit (`render_max_storage_mb`, default 2000 MB).
- Button "Archive all now" for an immediate run over all enabled sources
  (`POST /api/archive/render`).
- New unit, API and click tests (desktop and phone) for the archive.

### Changed

- The per-region button "🛰 FCI archive" is now "🗄 Archive" and opens the
  generalized archive viewer: source and image type can be chosen, images
  are already rendered, so browsing is as fast as the normal viewer and
  time-lapse works here too. Format selection (JPEG/PNG) is available as
  well. Deep links carry `#archive=<region>&s=<source>&c=<image type>&f=<format>`.
- `GET /api/regions/{region}/archive` now returns `sources`, `composites`
  and `images` (the old key `slots` remains as an alias); images are served
  from `GET /regions/{region}/archive/{source}/{image type}/{scan}.{png|jpg}`.
  Archived MTG FCI raw scans that have not been rendered yet are still
  listed and rendered on request.
- `GET /api/archive` and `POST /api/storage` also cover the rendered-image
  archive (`render_all`, `render_retention_hours`, `render_max_storage_mb`).
- Archived images of a region are discarded when its cut-out changes
  (position, radius, size, country borders).

## [1.0.0] — 2026-09-30

First public release.

### Added

- Versioning: `__version__` in `src/ha_satimage/__init__.py` and
  `pyproject.toml`, shown in the web UI footer and via `GET /api/version`;
  release tags `vX.Y.Z` are built into image tags `X.Y.Z`, `X.Y`, `latest`.
- Optional Watchtower service in `docker-compose.yml` for automatic updates
  (label-scoped, configurable poll interval, `--cleanup`).
- Local, persistent configuration: all state in a host directory mounted to
  `/data`, configurable via `HOST_DATA_DIR` (default `./data`).

### Changed

- Web UI, code and documentation translated to English.
- Public GHCR image; no registry login required.
