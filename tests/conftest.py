"""Shared fixtures: real uvicorn server with an isolated data directory.

The server deliberately runs as a separate process instead of via
``TestClient``: ``ha_satimage.main`` reads paths and configuration at
import time, and the click tests need a real HTTP server for the browser
anyway.
"""

from __future__ import annotations

import json
import io
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

from ha_satimage.config import default_config


def _dummy_config() -> dict:
    """Default regions, but using the test source instead of real EUMETSAT data.

    This way the server renders frames immediately without credentials, and
    no test accidentally triggers downloads from the Data Store.
    """
    config = default_config().model_dump()
    for entry in config["sources"]["catalog"]:
        entry["enabled"] = entry["id"] == "dummy"
    for region in config["regions"]:
        region["source"] = "dummy"
    return config


@dataclass
class LiveServer:
    url: str
    data_dir: Path
    storage_root: Path

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config.yaml"

    def frames(self, region: str) -> list[dict]:
        return httpx.get(f"{self.url}/api/regions/{region}/frames").json()["frames"]

    def ensure_frames(self, region: str, count: int, timeout: float = 20.0) -> list[dict]:
        """Trigger render runs until at least ``count`` frames exist.

        Repeats the trigger since colliding runs (global render lock) are
        deliberately skipped.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frames = self.frames(region)
            if len(frames) >= count:
                return frames
            httpx.post(f"{self.url}/api/regions/{region}/refresh")
            time.sleep(0.3)
        raise AssertionError(f"{region}: weniger als {count} Frames nach {timeout}s")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_healthy(url: str, process: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"Server crashed on startup:\n{output}")
        try:
            if httpx.get(f"{url}/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("Server did not become reachable in time")


@pytest.fixture
def start_server(tmp_path: Path) -> Iterator[Callable[..., LiveServer]]:
    """Factory for server instances; ``env`` allows e.g. EUMETSAT overrides."""
    processes: list[subprocess.Popen] = []

    def _start(env: dict[str, str] | None = None) -> LiveServer:
        data_dir = tmp_path / f"data{len(processes)}"
        data_dir.mkdir()
        storage_root = tmp_path / f"mnt{len(processes)}"
        storage_root.mkdir()
        (data_dir / "config.yaml").write_text(
            yaml.safe_dump(_dummy_config(), sort_keys=False), encoding="utf-8"
        )
        port = _free_port()
        server_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("EUMETSAT_")
        }
        server_env.update(
            HA_SATIMAGE_DATA_DIR=str(data_dir),
            HA_SATIMAGE_CONFIG=str(data_dir / "config.yaml"),
            HA_SATIMAGE_STORAGE_ROOTS=str(storage_root),
            # Never hit the real EUMETSAT API: closed port as default.
            HA_SATIMAGE_EUMETSAT_API="http://127.0.0.1:9",
            HA_SATIMAGE_MAINTENANCE_URL="http://127.0.0.1:9/rss-schedule",
        )
        server_env.update(env or {})
        process = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "ha_satimage.main:app",
                "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
            ],
            env=server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        processes.append(process)
        url = f"http://127.0.0.1:{port}"
        _wait_until_healthy(url, process)
        return LiveServer(url=url, data_dir=data_dir, storage_root=storage_root)

    yield _start

    for process in processes:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def live_server(start_server: Callable[..., LiveServer]) -> LiveServer:
    return start_server()


@pytest.fixture
def fci_server(tmp_path, monkeypatch):
    """Real HTTP app with retained raw FCI slots; only the costly renderer is fake."""
    import uvicorn
    from PIL import Image

    from ha_satimage import main
    from ha_satimage.buffer import BufferManager
    from ha_satimage.config import AppConfig
    from ha_satimage.sources import RenderedFrame

    config = AppConfig(**_dummy_config())
    config.storage.frames_dir = str(tmp_path / "frames")
    frames = Path(config.storage.frames_dir)
    names = ["20260926T235959Z", "20260927T000959Z"]
    for name in names:
        directory = frames / "_archive" / "EO_EUM_DAT_0662" / name
        directory.mkdir(parents=True)
        end = datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        # Legacy raw directory and metadata disagree; precision and offset also differ.
        end = (end + timedelta(seconds=37, microseconds=395000)).astimezone(timezone(timedelta(hours=2)))
        (directory / "meta.json").write_text(json.dumps({
            "product_id": name, "sensing_start": end.isoformat(), "sensing_end": end.isoformat(),
        }))
        for chunk in range(32, 41):
            (directory / f"chunk_{chunk:04d}.nc").write_bytes(b"fake chunk")
    calls = []

    def render(slot, region, composite, collection=None):
        calls.append((slot.name, region.name, composite))
        image = io.BytesIO()
        Image.new("RGB", (40, 40), "red").save(image, format="PNG")
        return RenderedFrame(image.getvalue(), datetime.now(timezone.utc))

    monkeypatch.setattr(main.config_store, "get", lambda: config)
    monkeypatch.setattr(main, "buffer_manager", BufferManager(frames))
    # The web app must never render; any render (scheduler only) is recorded.
    monkeypatch.setattr("ha_satimage.scheduler.render_fci_slot", render)
    server = uvicorn.Server(uvicorn.Config(
        main.app, host="127.0.0.1", port=_free_port(), lifespan="off", log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.config.port}"
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("FCI test server did not start")
            time.sleep(0.01)
        yield url, calls, frames, names
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


FAKE_COLLECTIONS = {
    "EO:EUM:DAT:MSG:HRSEVIRI": "High Rate SEVIRI Level 1.5 Image Data - MSG - 0 degree",
    "EO:EUM:DAT:MSG:MSG15-RSS": "Rapid Scan High Rate SEVIRI Level 1.5 Image Data - MSG",
    "EO:EUM:DAT:MSG:HRSEVIRI-IODC": "High Rate SEVIRI Level 1.5 Image Data - MSG - Indian Ocean",
    "EO:EUM:DAT:0662": "FCI Level 1c Normal Resolution Image Data - MTG - 0 degree",
    "EO:EUM:DAT:0665": "FCI Level 1c High Resolution Image Data - MTG - 0 degree",
    "EO:EUM:DAT:0678": "Cloud Mask (netCDF) - MTG - 0 degree",
}
FAKE_LATEST = "2026-09-26T10:30:10.395Z"


class _FakeEumetsat(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D401 - still
        pass

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/data/browse/1.0.0/collections":
            self._json({"links": [
                {"title": cid, "datasetTitle": title, "numberOfProducts": 1}
                for cid, title in FAKE_COLLECTIONS.items()
            ]})
        elif parsed.path == "/data/search-products/1.0.0/os":
            self._json({"properties": {"parameters": [
                {"name": "dtstart", "maxInclusive": FAKE_LATEST}
            ]}, "features": []})
        else:
            self.send_error(404)


@pytest.fixture
def fake_eumetsat() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeEumetsat)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


class _FakeMaintenancePage(BaseHTTPRequestHandler):
    """RSS schedule page with a maintenance window around "now"."""

    def log_message(self, *args) -> None:  # noqa: D401 - still
        pass

    def do_GET(self) -> None:  # noqa: N802
        now = datetime.now(timezone.utc)
        fmt = "%Y/%m/%d %H:%M"
        body = (
            "<table><tr><th>Start</th><th>End</th></tr><tr>"
            f"<td>{(now - timedelta(hours=1)).strftime(fmt)} UTC</td>"
            f"<td>{(now + timedelta(hours=2)).strftime(fmt)} UTC</td></tr></table>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_maintenance() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeMaintenancePage)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/rss-schedule"
    server.shutdown()
