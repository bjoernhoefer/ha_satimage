"""Tests for the derived media cache (thumbnails, animations)."""

from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from ha_satimage import media
from ha_satimage.buffer import RingBuffer
from ha_satimage.resources import HEAVY_WORK_LOCK
from ha_satimage.scheduler import RenderScheduler
from ha_satimage.sources import satpy_render

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _png(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(out, format="PNG")
    return out.getvalue()


def _buffer(tmp_path, frames: int = 3, max_frames: int = 3) -> RingBuffer:
    buffer = RingBuffer(tmp_path / "wien", max_frames=max_frames)
    for i in range(frames):
        buffer.add_frame(_png((40 * i, 0, 0)), timestamp=T0 + timedelta(minutes=5 * i))
    return buffer


def test_jpeg_is_cached_and_removed_with_its_frame(tmp_path, monkeypatch):
    buffer = _buffer(tmp_path)
    oldest = buffer.get(2)
    thumb = media.jpeg(buffer, oldest, 32)
    assert Image.open(io.BytesIO(thumb)).size == (32, 24)
    path = media.jpeg_path(buffer, oldest, 32)
    assert path.exists()

    # Second call only reads the file.
    monkeypatch.setattr(media, "to_jpeg", lambda *a: (_ for _ in ()).throw(AssertionError))
    assert media.jpeg(buffer, oldest, 32) == thumb

    # Frame drops out of the ring buffer -> thumbnail is cleaned up too.
    buffer.add_frame(_png((0, 0, 200)), timestamp=T0 + timedelta(minutes=30))
    assert buffer.by_filename(oldest.filename) is None
    assert not path.exists()


def test_animation_is_cached_until_frames_change(tmp_path, monkeypatch):
    buffer = _buffer(tmp_path)
    calls = []
    original = media.ENCODERS["gif"]

    def counting(images, fps):
        images = list(images)
        calls.append(len(images))
        return original(images, fps)

    monkeypatch.setitem(media.ENCODERS, "gif", counting)
    first = media.animation(buffer, "gif", 2.0)
    assert first.startswith(b"GIF")
    assert media.animation(buffer, "gif", 2.0) == first
    assert calls == [3]

    buffer.add_frame(_png((0, 200, 0)), timestamp=T0 + timedelta(minutes=30))
    media.animation(buffer, "gif", 2.0)
    assert calls == [3, 3]
    # Only the current animation remains.
    assert len(list(media.cache_dir(buffer).glob("animation-*.gif"))) == 1

    # Different frame rate -> new animation.
    media.animation(buffer, "gif", 4.0)
    assert calls == [3, 3, 3]


def test_prewarm_skips_gif_for_long_history(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "GIF_PREWARM_MAX_FRAMES", 2)
    buffer = _buffer(tmp_path)
    media.prewarm(buffer, 2.0)
    assert not list(media.cache_dir(buffer).glob("animation-*.gif"))
    assert list(media.cache_dir(buffer).glob("animation-*.mp4"))


def test_animation_without_frames_is_none(tmp_path):
    assert media.animation(RingBuffer(tmp_path / "leer"), "gif", 2.0) is None


def test_prewarm_creates_thumbnail_jpeg_and_animations(tmp_path):
    buffer = _buffer(tmp_path)
    media.prewarm(buffer, 2.0)
    latest = buffer.latest()
    assert media.jpeg_path(buffer, latest, media.THUMB_WIDTH).exists()
    assert media.jpeg_path(buffer, latest).exists()
    assert list(media.cache_dir(buffer).glob("animation-*.gif"))
    assert list(media.cache_dir(buffer).glob("animation-*.mp4"))


def test_scheduler_prewarms_after_new_frame(tmp_path, monkeypatch):
    from ha_satimage.buffer import BufferManager
    from ha_satimage.config import ConfigStore
    from ha_satimage.sources import RenderedFrame
    from ha_satimage.status import StatusStore

    class PngSource:
        def render(self, region, config, last_sensing=None):
            return RenderedFrame(_png((10, 20, 30)), T0)

    store = ConfigStore(tmp_path / "config.yaml")
    buffers = BufferManager(tmp_path / "frames")
    scheduler = RenderScheduler(store, buffers, StatusStore())
    monkeypatch.setattr("ha_satimage.scheduler.get_source", lambda name: PngSource())
    scheduler._run_region("wien")
    job = scheduler._scheduler.get_job("media-wien")
    assert job is not None  # separate job, does not block the render run
    assert job.executor == "media"
    # A prewarm queued behind a running one must not be dropped.
    assert job.max_instances >= 2
    job.func(*job.args)
    buffer = buffers.get("wien", 12, 500)
    assert media.jpeg_path(buffer, buffer.latest(), media.THUMB_WIDTH).exists()
    assert list(media.cache_dir(buffer).glob("animation-*.gif"))


def test_prewarm_after_relocation_does_not_recreate_old_region_dir(tmp_path):
    from ha_satimage.buffer import BufferManager

    buffers = BufferManager(tmp_path / "old")
    stale = buffers.get("wien", 12, 500)
    stale.add_frame(_png((10, 20, 30)), timestamp=T0)
    buffers.relocate(tmp_path / "new", move_existing=True)
    assert not stale.region_dir.exists()

    media.prewarm(stale, 2.0)  # queued before the move

    assert not stale.region_dir.exists()
    moved = buffers.get("wien", 12, 500)
    assert moved.latest() is not None


@pytest.mark.parametrize("other_work", ["gif", "mp4", "render"])
def test_heavy_work_is_serialized_across_regions(tmp_path, monkeypatch, other_work):
    first = _buffer(tmp_path / "first")
    second = _buffer(tmp_path / "second")
    encoding = threading.Event()
    release = threading.Event()
    attempted = threading.Event()
    entered = threading.Event()

    def blocked_encoder(images, fps):
        encoding.set()
        assert release.wait(5)
        return b"first"

    def other_encoder(*args):
        entered.set()
        return b"second"

    monkeypatch.setitem(media.ENCODERS, "gif", blocked_encoder)
    monkeypatch.setitem(media.ENCODERS, "mp4", other_encoder)
    monkeypatch.setattr(satpy_render, "_run_once", other_encoder)

    def other():
        attempted.set()
        if other_work == "render":
            return satpy_render.render_in_subprocess(object())
        return media.animation(second, other_work, 2)

    with ThreadPoolExecutor(2) as pool:
        first_job = pool.submit(media.animation, first, "gif", 2)
        try:
            assert encoding.wait(5)
            if other_work == "gif":
                monkeypatch.setitem(media.ENCODERS, "gif", other_encoder)
            second_job = pool.submit(other)
            assert attempted.wait(5)
            assert not entered.wait(0.1)
        finally:
            release.set()
        assert first_job.result(timeout=5) == b"first"
        assert second_job.result(timeout=5) == b"second"


def test_cached_animation_does_not_wait_for_heavy_work(tmp_path):
    buffer = _buffer(tmp_path)
    cached = media.animation(buffer, "gif", 2)
    with ThreadPoolExecutor(1) as pool:
        with HEAVY_WORK_LOCK:
            assert pool.submit(media.animation, buffer, "gif", 2).result(timeout=5) == cached


def test_encoder_error_releases_heavy_work_lock(tmp_path, monkeypatch):
    def fail(*args):
        raise ValueError("encoder failed")

    monkeypatch.setitem(media.ENCODERS, "gif", fail)
    with pytest.raises(ValueError, match="encoder failed"):
        media.animation(_buffer(tmp_path), "gif", 2)
    monkeypatch.setattr(satpy_render, "_run_once", lambda *args: b"rendered")
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(satpy_render.render_in_subprocess, object()).result(timeout=5) == b"rendered"


def test_render_can_reenter_scheduler_memory_lock(monkeypatch):
    monkeypatch.setattr(satpy_render, "_run_once", lambda *args: b"rendered")

    def run():
        with HEAVY_WORK_LOCK:
            return satpy_render.render_in_subprocess(object())

    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(run).result(timeout=5) == b"rendered"


def test_mp4_limits_ffmpeg_threads(tmp_path, monkeypatch):
    import imageio.v2 as imageio

    options = {}

    class Writer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def append_data(self, image):
            pass

    def writer(path, **kwargs):
        options.update(kwargs)
        path.write_bytes(b"mp4")
        return Writer()

    monkeypatch.setattr(imageio, "get_writer", writer)
    assert media._encode_mp4([Image.new("RGB", (16, 16))], 2) == b"mp4"
    assert options["ffmpeg_params"] == ["-threads", "1", "-filter_threads", "1"]


def test_download_has_capacity_while_heavy_work_is_busy(tmp_path, monkeypatch):
    from ha_satimage.buffer import BufferManager
    from ha_satimage.config import ConfigStore
    from ha_satimage.status import StatusStore

    scheduler = RenderScheduler(
        ConfigStore(tmp_path / "config.yaml"),
        BufferManager(tmp_path / "frames"),
        StatusStore(),
    )
    downloaded = threading.Event()
    monkeypatch.setattr(scheduler, "_tick_download", lambda source: downloaded.set())
    scheduler._reschedule()
    jobs = [job for job in scheduler._scheduler.get_jobs() if job.id.startswith("download-")]
    assert jobs
    assert all(job.executor == "download" for job in jobs)
    try:
        with HEAVY_WORK_LOCK:
            scheduler._scheduler.start()
            assert downloaded.wait(5)
    finally:
        scheduler._scheduler.shutdown(wait=True)
