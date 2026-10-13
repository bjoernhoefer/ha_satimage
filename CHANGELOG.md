# Changelog

All notable changes to this project are documented in this file.
The project follows [Semantic Versioning](https://semver.org/).

## [1.6.0] — 2026-10-13

### Changed

- All page sections of the web UI are foldable and collapsed by default, so
  the page loads smaller. Navigation links and `#sec-…` deep links open the
  matching section.

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
