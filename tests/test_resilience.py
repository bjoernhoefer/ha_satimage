"""Resource protection: render child processes, backoff, queues, storage pause."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np
import pytest

from ha_satimage.atomic import write_bytes_atomic, write_text_atomic
from ha_satimage.backoff import FailureBackoff, is_resource_error
from ha_satimage.buffer import BufferManager
from ha_satimage.config import ConfigStore
from ha_satimage.scheduler import RenderScheduler
from ha_satimage.sources import RenderedFrame, RenderError
from ha_satimage.sources import satpy_render
from ha_satimage.status import StatusStore
from ha_satimage.storage_guard import StorageGuardError

PNG = satpy_render.PNG_SIGNATURE + b"\x00" * 20 + satpy_render.PNG_END
SENSING = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)


# -- Render child process -----------------------------------------------------------
def _worker_ok(request, connection):
    connection.send(("ok", (PNG, SENSING), 1234))
    connection.close()


def _worker_incomplete(request, connection):
    connection.send(("ok", (PNG[:-4], SENSING), None))
    connection.close()


def _worker_exit(request, connection):
    os._exit(3)


def _worker_sigkill(request, connection):
    os.kill(os.getpid(), signal.SIGKILL)


def _worker_sleep(request, connection):
    time.sleep(30)


def _worker_memory_error(request, connection):
    connection.send(("error", "MemoryError: not enough memory", None))
    connection.close()


@pytest.fixture
def child(monkeypatch):
    """Real child processes (fork: the patched worker reaches the child)."""
    monkeypatch.setenv("HA_SATIMAGE_RENDER_START_METHOD", "fork")
    monkeypatch.setenv("HA_SATIMAGE_RENDER_MEMORY_MB", "0")
    monkeypatch.setattr(satpy_render, "RETRY_DELAY_SECONDS", 0)
    satpy_render.STATS.reset()

    def use(worker):
        monkeypatch.setattr(satpy_render, "_worker", worker)

    return use


def test_successful_render_is_counted(child):
    child(_worker_ok)
    assert satpy_render.render_in_subprocess(object(), timeout=20) == (PNG, SENSING)
    stats = satpy_render.STATS.snapshot()
    assert stats["runs"] == 1 and stats["succeeded"] == 1 and stats["active"] == 0
    assert stats["max_parallel"] == 1
    assert stats["last"]["outcome"] == "ok"


def test_incomplete_output_is_not_accepted(child):
    child(_worker_incomplete)
    with pytest.raises(satpy_render.SatpyRenderError, match="incomplete"):
        satpy_render.render_in_subprocess(object(), timeout=20)
    assert satpy_render.STATS.snapshot()["failures"] == {"incomplete": 1}


def test_crash_is_retried_once_with_delay(child, monkeypatch):
    child(_worker_exit)
    sleeps = []
    monkeypatch.setattr(satpy_render.time, "sleep", sleeps.append)
    with pytest.raises(satpy_render.RenderProcessCrashed, match="exit code 3"):
        satpy_render.render_in_subprocess(object(), timeout=20)
    assert satpy_render.STATS.snapshot()["runs"] == 2
    assert sleeps == [0]


def test_sigkill_is_a_resource_error_without_retry(child):
    child(_worker_sigkill)
    with pytest.raises(satpy_render.RenderResourceError, match="SIGKILL"):
        satpy_render.render_in_subprocess(object(), timeout=20)
    stats = satpy_render.STATS.snapshot()
    assert stats["runs"] == 1 and stats["failures"] == {"oom_killed": 1}


def test_timeout_kills_the_child_without_retry(child):
    child(_worker_sleep)
    started = time.monotonic()
    with pytest.raises(satpy_render.RenderResourceError, match="time limit"):
        satpy_render.render_in_subprocess(object(), timeout=0.5)
    assert time.monotonic() - started < 10
    assert satpy_render.STATS.runs == 1
    assert satpy_render.STATS.failures == {"timeout": 1}
    assert satpy_render.STATS.active == 0


def test_memory_budget_stops_the_child(child, monkeypatch):
    child(_worker_sleep)
    monkeypatch.setenv("HA_SATIMAGE_RENDER_MEMORY_MB", "1")
    with pytest.raises(satpy_render.RenderResourceError, match="memory budget"):
        satpy_render.render_in_subprocess(object(), timeout=20)
    assert satpy_render.STATS.failures == {"memory_budget": 1}


def test_memory_error_in_child_is_a_resource_error(child):
    child(_worker_memory_error)
    with pytest.raises(satpy_render.RenderResourceError):
        satpy_render.render_in_subprocess(object(), timeout=20)


def test_memory_budget_from_cgroup(tmp_path):
    (tmp_path / "memory.max").write_text(f"{768 * 2**20}\n")
    assert satpy_render.memory_budget_bytes({}, tmp_path) == (768 - 192) * 2**20
    assert satpy_render.memory_budget_bytes({"HA_SATIMAGE_RENDER_MEMORY_MB": "400"}, tmp_path) == 400 * 2**20
    assert satpy_render.memory_budget_bytes({"HA_SATIMAGE_RENDER_MEMORY_MB": "0"}, tmp_path) is None
    (tmp_path / "memory.max").write_text("max\n")
    assert satpy_render.memory_budget_bytes({}, tmp_path) is None
    (tmp_path / "memory.events").write_text("low 0\noom 2\noom_kill 3\n")
    assert satpy_render.cgroup_oom_kills(tmp_path) == 3


# -- Coarsening (bounded resampling input) ---------------------------------------------
def test_coarsen_factor_limits_source_pixels():
    assert satpy_render.coarsen_factor(1000, 1000, min_pixels=0) == 1
    assert satpy_render.coarsen_factor(4000, 1000, min_pixels=0) == 1
    assert satpy_render.coarsen_factor(9000, 1000, min_pixels=0) == 2
    # Regular region with HRV (Wien, 300 km at 200 px): small window, kept.
    assert satpy_render.coarsen_factor(181 * 429 * 3, 200 * 200) == 1
    # Europe overview from the 1 km HRV grid: ~12.4 Mpx for 1.8 Mpx output.
    factor = satpy_render.coarsen_factor(12_400_000, 1400 * 1280)
    assert factor == 2 and 12_400_000 / factor**2 <= 4 * 1400 * 1280
    assert satpy_render.coarsen_factor(10**8, 100, max_ratio=0) == 1


def test_coarsen_keeps_extent_and_averages():
    xr = pytest.importorskip("xarray")
    from pyresample.geometry import AreaDefinition

    area = AreaDefinition("a", "a", "a", "+proj=laea +lat_0=50 +lon_0=10", 10, 9,
                          (-5000, -4500, 5000, 4500))
    values = np.arange(90, dtype=np.float32).reshape(9, 10)
    data = xr.DataArray(values, dims=("y", "x"), attrs={"area": area, "name": "vis"},
                        coords={"y": np.arange(9), "x": np.arange(10), "crs": 0})
    coarse = satpy_render._coarsen(data, 2)
    assert coarse.shape == (4, 5)
    assert coarse.dtype == np.float32
    assert float(coarse[0, 0]) == pytest.approx(values[:2, :2].mean())
    new_area = coarse.attrs["area"]
    assert (new_area.width, new_area.height) == (5, 4)
    trimmed = area[0:8, 0:10]
    assert new_area.area_extent == pytest.approx(trimmed.area_extent)
    assert new_area.pixel_size_x == pytest.approx(2 * area.pixel_size_x)
    assert coarse.attrs["name"] == "vis"


# -- Backoff --------------------------------------------------------------------------
def test_backoff_doubles_up_to_maximum_and_resets():
    backoff = FailureBackoff(timedelta(minutes=15), timedelta(hours=1))
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    key = ("wien", "msg_seviri", "x")
    delays = [backoff.failure(key, "oom", now) - now for _ in range(4)]
    assert delays == [timedelta(minutes=15), timedelta(minutes=30), timedelta(hours=1), timedelta(hours=1)]
    assert backoff.blocked_until(key, now) is not None
    assert backoff.blocked_until(key, now + timedelta(hours=2)) is None
    assert backoff.snapshot(now)[0]["failures"] == 4
    backoff.success(key)
    assert backoff.blocked_until(key, now) is None


def test_resource_error_is_found_in_the_cause_chain():
    try:
        try:
            raise satpy_render.RenderResourceError("oom")
        except satpy_render.SatpyRenderError as exc:
            raise RenderError(str(exc)) from exc
    except RenderError as wrapped:
        assert is_resource_error(wrapped)
    assert not is_resource_error(RenderError("401"))


# -- Scheduler --------------------------------------------------------------------------
class OomSource:
    def __init__(self):
        self.calls = 0
        self.fail = True

    def render(self, region, config, last_sensing=None):
        self.calls += 1
        if self.fail:
            try:
                raise satpy_render.RenderResourceError("killed by the kernel OOM killer")
            except satpy_render.RenderResourceError as exc:
                raise RenderError(str(exc)) from exc
        return RenderedFrame(PNG, SENSING)

    def fetch(self, entry, config):
        self.calls += 1
        raise RenderError("503 Service Unavailable")


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.yaml")
    status = StatusStore()
    scheduler = RenderScheduler(store, BufferManager(tmp_path / "frames"), status)
    source = OomSource()
    monkeypatch.setattr("ha_satimage.scheduler.get_source", lambda name: source)
    scheduler.source, scheduler.status = source, status
    yield scheduler
    if scheduler._scheduler.running:
        scheduler.shutdown()


def test_resource_failure_pauses_the_region_instead_of_retrying(scheduler):
    scheduler._run_region("wien")
    assert scheduler.source.calls == 1
    assert "paused until" in scheduler.status.get("wien").last_error
    scheduler._run_region("wien")  # next scan: no new attempt
    assert scheduler.source.calls == 1
    assert scheduler.diagnostics()["backoff"][0]["key"].startswith("wien/")

    # "Refresh now" is an explicit retry and ends the pause.
    scheduler.source.fail = False
    scheduler.trigger_now("wien")
    scheduler._run_region("wien")
    assert scheduler.source.calls == 2
    assert scheduler.status.get("wien").last_error is None
    assert scheduler.diagnostics()["backoff"] == []


def test_queued_renders_are_deduplicated(scheduler, monkeypatch):
    runs = []
    monkeypatch.setattr(scheduler, "_run_region", runs.append)
    for _ in range(5):
        scheduler._queue_render("wien")
    scheduler._queue_render("mallorca")
    jobs = [j for j in scheduler._scheduler.get_jobs() if "now-" in j.id]
    assert len(jobs) == 2
    assert scheduler.diagnostics()["render_queue"] == ["mallorca", "wien"]
    scheduler._run_queued_region("wien")
    assert runs == ["wien"]
    scheduler._queue_render("wien")  # queued again after it started
    assert len([j for j in scheduler._scheduler.get_jobs() if "now-wien" in j.id]) == 2


def test_download_errors_back_off_exponentially(scheduler):
    state_due = []
    for _ in range(6):
        before = datetime.now(timezone.utc)
        assert scheduler.download("msg_seviri") is False
        state = scheduler._downloads["msg_seviri"]
        state_due.append(round((state.due - before).total_seconds() / 60))
    assert state_due == [5, 10, 20, 40, 60, 60]
    assert scheduler.diagnostics()["downloads"]["msg_seviri"]["consecutive_errors"] == 6


def test_storage_problem_pauses_downloads_and_renders(tmp_path, monkeypatch):
    problem = {"text": "Only 10 MiB free on /data"}

    def check():
        if problem["text"]:
            raise StorageGuardError(problem["text"])
        return []

    store = ConfigStore(tmp_path / "config.yaml")
    status = StatusStore()
    scheduler = RenderScheduler(store, BufferManager(tmp_path / "frames"), status, storage_check=check)
    source = OomSource()
    source.fail = False
    monkeypatch.setattr("ha_satimage.scheduler.get_source", lambda name: source)

    assert scheduler.check_storage() is False
    assert scheduler.download("msg_seviri") is False
    scheduler._run_region("wien")
    assert source.calls == 0
    assert "Paused" in status.get("wien").last_error
    assert scheduler.diagnostics()["storage"]["paused"] is True

    problem["text"] = None
    assert scheduler.check_storage() is True
    scheduler._run_region("wien")
    assert source.calls == 1
    assert scheduler.diagnostics()["storage"]["paused"] is False


# -- Files ------------------------------------------------------------------------------
def test_atomic_writes_leave_no_partial_file(tmp_path, monkeypatch):
    target = tmp_path / "index.json"
    write_text_atomic(target, "old")

    def broken_replace(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        write_bytes_atomic(target, b"new")
    monkeypatch.undo()
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["index.json"]


def test_corrupt_index_is_rebuilt_instead_of_deleting_frames(tmp_path):
    manager = BufferManager(tmp_path)
    buffer = manager.get("wien", 10, 500)
    for minute in range(3):
        buffer.add_frame(PNG, timestamp=SENSING + timedelta(minutes=minute))
    (tmp_path / "wien" / "_index.json").write_text("{ broken")
    reopened = BufferManager(tmp_path).get("wien", 10, 500)
    assert len(reopened) == 3
    assert len(list((tmp_path / "wien").glob("*.png"))) == 3


def test_stdout_log_level_is_configurable(monkeypatch):
    import logging

    from ha_satimage import logbuffer

    monkeypatch.setenv(logbuffer.STDOUT_LEVEL_ENV, "warning")
    assert logbuffer.stdout_level() == logging.WARNING
    monkeypatch.setenv(logbuffer.STDOUT_LEVEL_ENV, "nonsense")
    assert logbuffer.stdout_level() == logging.INFO
    assert logbuffer.FILE_MAX_BYTES * (logbuffer.FILE_BACKUPS + 1) <= 5_000_000


# -- Server -----------------------------------------------------------------------------
def test_diagnostics_endpoint(live_server):
    response = httpx.get(f"{live_server.url}/api/diagnostics", timeout=10)
    response.raise_for_status()
    body = response.json()
    assert {"render", "render_queue", "backoff", "storage", "downloads"} <= set(body)
    assert body["storage"]["paused"] is False
    assert body["storage"]["paths"][0]["free_mb"] > 0
    assert (live_server.data_dir / ".instance.lock").exists()


def test_server_refuses_migrated_or_wrong_data_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / ".ha_satimage-migrated").write_text("{}")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EUMETSAT_", "HA_SATIMAGE_"))}
    env.update(HA_SATIMAGE_DATA_DIR=str(data), HA_SATIMAGE_CONFIG=str(data / "config.yaml"))
    result = subprocess.run(
        [sys.executable, "-m", "uvicorn", "ha_satimage.main:app", "--port", "0"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "migrated" in (result.stdout + result.stderr)
    assert not (data / "config.yaml").exists()
    assert not (data / "frames").exists()
