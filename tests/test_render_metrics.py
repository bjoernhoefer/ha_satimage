"""Render telemetry, including child death without a result."""

import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from ha_satimage.render_metrics import MAX_GROUPS, RenderMetrics, rss_bytes
from ha_satimage.sources import satpy_render


def test_samples_duration_failures_skips_and_context(monkeypatch):
    metrics = RenderMetrics()
    monkeypatch.setattr("ha_satimage.render_metrics.rss_bytes", lambda *args: 1024)
    with metrics.measure("preview", source="dummy", region="test", composite="natural"):
        with metrics.measure("subprocess"):
            pass
    with pytest.raises(ValueError):
        with metrics.measure("preview", source="dummy", region="test", composite="natural"):
            raise ValueError("sensitive text must not be retained")
    with pytest.raises(LookupError):
        with metrics.measure("preview", source="dummy", region="test", composite="natural",
                             skip_exceptions=(LookupError,)):
            raise LookupError()
    parent, child = metrics.snapshot()["processes"]
    assert (parent["runs"], parent["failures"], parent["skipped"], parent["active"]) == (3, 1, 1, 0)
    assert parent["rss_avg_bytes"] == parent["rss_min_bytes"] == parent["rss_max_bytes"] == 1024
    assert 0 <= parent["duration_min_seconds"] <= parent["duration_avg_seconds"] <= parent["duration_max_seconds"]
    assert child["source"] == "dummy"
    assert child["region"] == "test"
    assert child["composite"] == "natural"
    assert "sensitive" not in str(metrics.snapshot())


def test_missing_rss_is_not_zero_and_active_visible(monkeypatch):
    metrics = RenderMetrics()
    monkeypatch.setattr("ha_satimage.render_metrics.rss_bytes", lambda *args: None)
    with metrics.measure("preview"):
        row = metrics.snapshot()["processes"][0]
        assert row["active"] == 1
        assert row["duration_avg_seconds"] is None
    row = metrics.snapshot()["processes"][0]
    assert row["rss_samples"] == 0
    assert row["rss_avg_bytes"] is None
    assert "ha_satimage_render_rss_avg_bytes{" not in metrics.prometheus()


def test_concurrent_updates_and_bounded_cardinality():
    metrics = RenderMetrics()

    def run(index):
        with metrics.measure("preview", region=str(index % 4)):
            pass

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(run, range(32)))
    assert sum(row["runs"] for row in metrics.snapshot()["processes"]) == 32
    for index in range(MAX_GROUPS + 10):
        with metrics.measure("preview", region=f"extra-{index}"):
            pass
    rows = metrics.snapshot()["processes"]
    assert len(rows) <= MAX_GROUPS + 1
    assert any(row["kind"] == "overflow" for row in rows)
    assert all(row["active"] == 0 for row in rows)


def test_prometheus_escaping_and_types():
    metrics = RenderMetrics()
    with pytest.raises(RuntimeError):
        with metrics.measure("preview", region='a"b\\c\nd'):
            raise RuntimeError("do not expose")
    text = metrics.prometheus()
    assert '# TYPE ha_satimage_render_failures_total counter' in text
    assert '# TYPE ha_satimage_render_rss_avg_bytes gauge' in text
    assert 'region="a\\"b\\\\c\\nd"' in text
    assert "do not expose" not in text


def _successful_worker(request, connection):
    memory = bytearray(8 * 1024 * 1024)
    time.sleep(0.25)
    connection.send(("ok", (bytes(memory[:3]), "timestamp")))
    connection.close()


def _crashed_worker(request, connection):
    time.sleep(0.15)
    os._exit(1)


def _error_worker(request, connection):
    connection.send(("error", "render failed"))
    connection.close()


def _slow_worker(request, connection):
    time.sleep(0.25)
    connection.close()


@pytest.mark.parametrize("worker,error,attempts", [
    (_successful_worker, None, 1),
    (_crashed_worker, satpy_render.RenderProcessCrashed, 2),
    (_error_worker, satpy_render.SatpyRenderError, 1),
    (_slow_worker, satpy_render.RenderTimeoutError, 1),
])
def test_real_child_attempts(monkeypatch, worker, error, attempts):
    metrics = RenderMetrics()
    context = multiprocessing.get_context("spawn")

    class TestContext:
        Pipe = staticmethod(context.Pipe)

        @staticmethod
        def Process(*, target, args, daemon):
            return context.Process(target=worker, args=args, daemon=daemon)

    monkeypatch.setattr(satpy_render.multiprocessing, "get_context", lambda method: TestContext())
    monkeypatch.setattr(satpy_render, "render_metrics", metrics)
    request = satpy_render.RenderRequest(
        "reader", (), "natural", 48, 16, 200, 40, 40, "test")
    with metrics.measure("preview", source="dummy", region="test"):
        if error:
            with pytest.raises(error):
                satpy_render.render_in_subprocess(
                    request, timeout=0.01 if worker is _slow_worker else 3)
        else:
            assert satpy_render.render_in_subprocess(request)[1] == "timestamp"
    child = next(row for row in metrics.snapshot()["processes"] if row["kind"] == "subprocess")
    assert child["runs"] == attempts
    assert child["failures"] == (attempts if error else 0)
    assert child["active"] == 0
    assert child["source"] == "dummy"
    if worker in (_successful_worker, _crashed_worker):
        assert child["rss_max_bytes"] > 0
    assert not multiprocessing.active_children()


def test_linux_rss_and_missing_process():
    assert rss_bytes(os.getpid()) > 0
    assert rss_bytes(999999999) is None


def test_metrics_api_and_prometheus(live_server):
    live_server.ensure_frames("wien", 1)
    response = httpx.get(live_server.url + "/api/render-metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["sample_interval_seconds"] == 0.1
    row = next(row for row in data["processes"] if row["region"] == "wien")
    assert row["kind"] == "preview"
    assert row["runs"] >= 1
    assert row["failures"] == 0
    assert row["rss_avg_bytes"] > 0
    response = httpx.get(live_server.url + "/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "version=0.0.4" in response.headers["content-type"]
    assert 'ha_satimage_render_attempts_total{kind="preview"' in response.text
