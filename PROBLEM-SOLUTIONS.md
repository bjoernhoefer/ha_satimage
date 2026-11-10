# Problem → solution knowledge base

Known errors from the logs of the running ha_satimage containers, their
cause and how to fix them. The hourly log monitor
(`.github/workflows/log-monitor.yml`, `scripts/log_monitor.py`) matches every
ERROR/CRITICAL log entry against the **Pattern** of the entries below:

- **Match:** the monitor's issue (label `log-monitor`, `known-problem`)
  links the entry and quotes its solution, so the fix can be applied right
  away.
- **No match:** the issue gets the label `needs-analysis` and, if
  configured, is assigned to the Copilot coding agent. The agent analyses
  the error, fixes the code if possible and **adds an entry here** in the
  same pull request, so the next occurrence is recognised.

## Entry format

Every entry is a level-2 heading `## PS-<number>: <title>` followed by these
bullet points (the monitor parses them; keep the exact labels):

- `- **Pattern:**` a Python regular expression in backticks, matched
  case-insensitively against the full log message (incl. traceback). Make it
  specific enough not to swallow unrelated errors.
- `- **Symptom:**` what is seen in the log/UI.
- `- **Cause:**` the root cause.
- `- **Solution:**` concrete steps (configuration, host command or code
  change). Keep it actionable.
- `- **Fixed in:**` version/PR of a code fix, or `—` for operational
  problems.

Numbers are never reused; obsolete entries stay (marked as such in the
title) so old issues keep their links.

## PS-001: EUMETSAT credentials missing

- **Pattern:** `No EUMETSAT credentials configured`
- **Symptom:** `Download <source> failed: No EUMETSAT credentials configured
  (web UI or environment variables)`; no new frames.
- **Cause:** neither the web UI configuration nor `EUMETSAT_CONSUMER_KEY` /
  `EUMETSAT_CONSUMER_SECRET` in `.env` contain credentials.
- **Solution:** enter consumer key/secret in the web UI ("EUMETSAT access")
  or set both variables in the host `.env` and recreate the container
  (`docker compose up -d`). Keys: <https://api.eumetsat.int/api-key/>.
- **Fixed in:** —

## PS-002: EUMETSAT Data Store search failed

- **Pattern:** `Data Store search failed`
- **Symptom:** `Download <source> failed: Data Store search failed: …`.
- **Cause:** invalid/revoked API key (`401`/`invalid_client`), EUMETSAT
  outage or network/DNS problem on the host. Single occurrences are usually
  transient; the scheduler retries after ≤ 5 min.
- **Solution:** if it persists: check the key in the web UI (re-enter
  consumer key/secret), check <https://uns.eumetsat.int/> for outages and
  `curl -sI https://api.eumetsat.int` on the host. A `401` means the key must
  be regenerated.
- **Fixed in:** —

## PS-003: No product in the collection

- **Pattern:** `No product in \S+ within the last \d+ hours`
- **Symptom:** `Download <source> failed: No product in EO:EUM:DAT:… within
  the last N hours`.
- **Cause:** the collection delivers no data (satellite manoeuvre,
  unannounced outage, collection retired) or the catalogue entry points to a
  wrong collection.
- **Solution:** run "Sync now" in the source table; if the collection is
  reported missing, adopt the replacement collection or disable the source.
  Planned RSS outages are handled by the maintenance schedule (no error).
- **Fixed in:** —

## PS-004: Render process killed (memory)

- **Pattern:** `Render process (exceeded the memory budget|killed by the kernel OOM killer)`
- **Symptom:** `Rendering for <region> failed: Render process exceeded the
  memory budget …` or `… killed by the kernel OOM killer …` followed by
  `paused until HH:MM UTC`.
- **Cause:** the composite/region needs more memory than the container limit
  (`768M`) minus 192 MiB allows — typically large radii with HRV/1 km
  composites, or FCI `true_color`/`airmass`.
- **Solution:** choose a lighter image type (e.g.
  `natural_color_raw_with_night_ir`), reduce the radius/size of the region,
  or raise `deploy.resources.limits.memory` / `HA_SATIMAGE_RENDER_MEMORY_MB`
  if the host has the memory. Check `GET /api/diagnostics` for peak RSS.
- **Fixed in:** —

## PS-005: Rendering exceeded the time limit

- **Pattern:** `Rendering exceeded the time limit`
- **Symptom:** `Rendering for <region> failed: Rendering exceeded the time
  limit of Ns - paused until …`.
- **Cause:** host overloaded (other containers, swapping, slow storage) or
  an expensive composite.
- **Solution:** check host load and swap (`scripts/ha_measure.sh`), use a
  lighter composite or smaller region. Persistent on a Pi: see README
  "Raspberry Pi resource protection and recovery".
- **Fixed in:** —

## PS-006: Storage check failed

- **Pattern:** `Storage (check failed|problem - downloads and renders paused)`
- **Symptom:** `Storage check failed - not starting: …` (container exits
  with code 78) or `Storage problem - downloads and renders paused: …`.
- **Cause:** the data directory is not on the expected filesystem (NVMe not
  mounted, UUID mismatch), not writable, or free space is below
  `HA_SATIMAGE_MIN_FREE_MB`.
- **Solution:** on the host: `findmnt <HOST_DATA_DIR>`, `df -h`, check
  `HA_SATIMAGE_REQUIRE_FS_UUID`; remount the disk or free space, then
  `docker compose up -d`. Never point the data directory at the SD card.
  Details: `docs/storage.md`.
- **Fixed in:** —

## PS-007: Composite could not be generated

- **Pattern:** `Composite '[^']+' could not be generated`
- **Symptom:** `Rendering for <region> failed: Composite '<name>' could not
  be generated`.
- **Cause:** the image type is not available for the source/collection (e.g.
  natural colour on FCI HRFI) or needs data that is missing (e.g.
  `natural_color_with_night_ir` downloads NASA BlackMarble at runtime and
  fails on the hash).
- **Solution:** select an image type offered by the UI for that source
  (`config.composites_for(driver, collection)`); never use
  `natural_color_with_night_ir`.
- **Fixed in:** —

## PS-008: Log monitor cannot reach the service

- **Pattern:** `ha_satimage service unreachable`
- **Symptom:** issue "ha_satimage service unreachable on <target>" from the
  log monitor.
- **Cause:** the container is stopped, restarting (storage guard exit 78,
  crash loop) or its port is not `LOG_MONITOR_PORT` (default `6060`).
- **Solution:** on the host: `docker compose ps`, `docker compose logs
  --tail 200`; fix the reported problem (often PS-006) and run
  `docker compose up -d`. If the port differs, set the repository variable
  `LOG_MONITOR_PORT`.
- **Fixed in:** —
