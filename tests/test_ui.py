"""Click tests for the web UI (Playwright, real browser).

These tests guard the core functions of the configuration page so layout or
styling changes cannot silently break them. Selectors use ``data-testid``
attributes exclusively - the markup may change freely as long as these IDs
are kept.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yaml

# Skip locally without Playwright; in CI (HA_SATIMAGE_REQUIRE_UI_TESTS=1)
# a missing Playwright must fail hard instead of silently skipping.
if os.environ.get("HA_SATIMAGE_REQUIRE_UI_TESTS"):
    import playwright.sync_api as playwright_api
else:
    playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

pytestmark = pytest.mark.ui

_OPEN_FOLDS = """
document.addEventListener("DOMContentLoaded", () => {
  if (!window.__keepFolded)
    document.querySelectorAll("details.fold").forEach((d) => { d.open = true; });
});
"""


@pytest.fixture
def context(context, request):
    """Page sections are collapsed by default; open them for all tests
    except those marked with ``keep_folded``."""
    if not request.node.get_closest_marker("keep_folded"):
        context.add_init_script(_OPEN_FOLDS)
    return context


@pytest.fixture
def ui(page, live_server):
    """Opens the UI and fails as soon as JS errors occur."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(live_server.url + "/")
    yield page
    assert errors == [], f"JavaScript errors in the UI: {errors}"


def _stored_credentials(server) -> dict:
    return yaml.safe_load(server.config_file.read_text(encoding="utf-8"))["eumetsat"]


def test_page_shows_all_core_sections(ui):
    for testid in (
        "credentials-form",
        "consumer-key",
        "consumer-secret",
        "save-credentials",
        "region-wien",
        "region-mallorca",
        "refresh-wien",
        "sources-form",
        "poll-interval",
        "auto-sync-hours",
        "maintenance-check-hours",
        "sources-table",
        "sources-sync",
        "sources-json-details",
        "storage-path",
        "save-storage",
        "logs",
        "live-wien",
        "play-wien",
        "region-source-wien",
        "region-composite-wien",
        "region-borders-wien",
        "api-region",
        "api-link-latest",
        "location-add",
        "location-count",
        "location-edit-wien",
        "location-remove-wien",
    ):
        expect(ui.get_by_test_id(testid)).to_be_visible()
    expect(ui.get_by_test_id("consumer-key")).to_be_editable()
    expect(ui.get_by_test_id("consumer-secret")).to_be_editable()
    expect(ui.get_by_test_id("consumer-secret")).to_have_attribute("type", "password")


def test_satellite_favicon_is_loaded(ui):
    expect(ui.locator('link[rel="icon"]')).to_have_attribute("href", "/favicon.svg")

    response = ui.request.get(ui.url.rstrip("/") + "/favicon.svg")
    assert response.status == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "🛰" in response.text()


def test_enter_and_save_credentials(ui, live_server):
    ui.get_by_test_id("consumer-key").fill("my-consumer-key")
    ui.get_by_test_id("consumer-secret").fill("my-consumer-secret-ABCD")
    ui.get_by_test_id("save-credentials").click()

    expect(ui.get_by_test_id("credentials-message")).to_have_text("Credentials saved.")
    expect(ui.get_by_test_id("secret-masked")).to_have_text(re.compile(r"^\*+ABCD$"))
    expect(ui.get_by_test_id("consumer-secret")).to_have_value("")
    assert _stored_credentials(live_server) == {
        "consumer_key": "my-consumer-key",
        "consumer_secret": "my-consumer-secret-ABCD",
    }

    # After reload: key visible, secret only masked, never in plain text.
    ui.reload()
    expect(ui.get_by_test_id("consumer-key")).to_have_value("my-consumer-key")
    expect(ui.get_by_test_id("secret-masked")).to_have_text(re.compile(r"^\*+ABCD$"))
    expect(ui.get_by_test_id("consumer-secret")).to_have_value("")
    assert "my-consumer-secret" not in ui.content()


def test_saving_without_new_secret_keeps_existing_secret(ui, live_server):
    ui.get_by_test_id("consumer-key").fill("key-1")
    ui.get_by_test_id("consumer-secret").fill("first-secret-1111")
    ui.get_by_test_id("save-credentials").click()
    expect(ui.get_by_test_id("credentials-message")).to_have_text("Credentials saved.")

    ui.reload()
    ui.get_by_test_id("consumer-key").fill("key-2")
    ui.get_by_test_id("save-credentials").click()
    expect(ui.get_by_test_id("credentials-message")).to_have_text("Credentials saved.")

    assert _stored_credentials(live_server) == {
        "consumer_key": "key-2",
        "consumer_secret": "first-secret-1111",
    }


def test_env_override_locks_fields(page, start_server):
    server = start_server(
        env={"EUMETSAT_CONSUMER_KEY": "env-key", "EUMETSAT_CONSUMER_SECRET": "env-secret-7777"}
    )
    page.goto(server.url + "/")
    expect(page.get_by_test_id("env-override-notice")).to_be_visible()
    expect(page.get_by_test_id("consumer-key")).to_be_disabled()
    expect(page.get_by_test_id("consumer-secret")).to_be_disabled()
    expect(page.get_by_test_id("secret-masked")).to_have_text(re.compile(r"7777$"))


def test_change_poll_interval(ui, live_server):
    ui.get_by_test_id("poll-interval").fill("5")
    ui.get_by_test_id("save-sources").click()
    expect(ui.get_by_test_id("sources-message")).to_have_text("Settings saved.")

    ui.reload()
    expect(ui.get_by_test_id("poll-interval")).to_have_value("5")
    stored = yaml.safe_load(live_server.config_file.read_text(encoding="utf-8"))
    assert stored["sources"]["poll_interval_minutes"] == 5


def test_refresh_button_stays_on_page_and_renders_frame(ui, live_server):
    ui.get_by_test_id("refresh-mallorca").click()
    expect(ui.get_by_test_id("refresh-message-mallorca")).to_have_text("Refresh triggered.")
    assert ui.url.rstrip("/") == live_server.url

    # The scheduler renders in the background; afterwards the preview must load.
    def preview_loaded() -> bool:
        ui.reload()
        return ui.get_by_test_id("preview-mallorca").evaluate(
            "img => img.complete && img.naturalWidth > 0"
        )

    for _ in range(50):
        if preview_loaded():
            break
        ui.wait_for_timeout(200)
    else:
        pytest.fail("Preview image for mallorca was not loaded")


# --- Helpers -------------------------------------------------------------

def _viewport_filled(page, testid: str = "viewer-image") -> None:
    """The image must fill the entire viewport (no small popup)."""
    box = page.get_by_test_id(testid).bounding_box()
    size = page.viewport_size
    assert box is not None
    assert abs(box["x"]) < 1 and abs(box["y"]) < 1
    assert abs(box["width"] - size["width"]) < 2
    assert abs(box["height"] - size["height"]) < 2


def _image_loaded(page, testid: str) -> None:
    page.wait_for_function(
        "id => { const i = document.querySelector(`[data-testid=\"${id}\"]`);"
        " return i && i.complete && i.naturalWidth > 0; }",
        arg=testid,
    )


def _stored(server) -> dict:
    return yaml.safe_load(server.config_file.read_text(encoding="utf-8"))


# --- Viewer / history / live ------------------------------------------

def test_preview_click_opens_fullscreen_viewer_with_history(ui, live_server):
    frames = live_server.ensure_frames("wien", 3)
    ui.reload()
    ui.get_by_test_id("preview-wien").click()

    viewer = ui.get_by_test_id("viewer")
    expect(viewer).to_be_visible()
    _image_loaded(ui, "viewer-image")
    _viewport_filled(ui)
    total = len(live_server.frames("wien"))
    assert total >= len(frames)
    expect(ui.get_by_test_id("viewer-position")).to_have_text(f"{total} / {total}")
    expect(ui.get_by_test_id("viewer-caption")).to_contain_text("latest image")
    expect(ui.get_by_test_id("viewer-next")).to_be_disabled()

    ui.get_by_test_id("viewer-prev").click()
    expect(ui.get_by_test_id("viewer-position")).to_have_text(f"{total - 1} / {total}")
    assert "view=wien" in ui.url and "i=1" in ui.url
    ui.keyboard.press("ArrowRight")
    expect(ui.get_by_test_id("viewer-position")).to_have_text(f"{total} / {total}")

    ui.keyboard.press("Escape")
    expect(viewer).to_be_hidden()
    assert "#" not in ui.url or ui.url.endswith("#")


def test_history_thumbnail_opens_that_frame(ui, live_server):
    live_server.ensure_frames("wien", 3)
    ui.reload()
    item = ui.get_by_test_id("history-item-wien-2")
    expect(item).to_be_visible()
    frames = live_server.frames("wien")
    item.click()
    expect(ui.get_by_test_id("viewer")).to_be_visible()
    expect(ui.get_by_test_id("viewer-position")).to_have_text(f"{len(frames) - 2} / {len(frames)}")
    src = ui.get_by_test_id("viewer-image").get_attribute("src")
    assert src == frames[2]["jpeg_url"]  # default: cached JPEG
    ui.get_by_test_id("viewer-close").click()
    expect(ui.get_by_test_id("viewer")).to_be_hidden()


def _zoom(page) -> float:
    return float(page.get_by_test_id("viewer").get_attribute("data-zoom") or 1)


def test_viewer_frame_dropdown_selects_frame(ui, live_server):
    live_server.ensure_frames("wien", 3)
    ui.reload()
    ui.get_by_test_id("preview-wien").click()
    expect(ui.get_by_test_id("viewer")).to_be_visible()
    frames = live_server.frames("wien")
    total = len(frames)
    select = ui.get_by_test_id("viewer-frame")
    expect(select).to_be_visible()
    expect(select.locator("option")).to_have_count(total)
    expect(select).to_have_value("0")
    expect(select.locator("option").first).to_contain_text("newest")

    select.select_option("2")
    expect(ui.get_by_test_id("viewer-position")).to_have_text(f"{total - 2} / {total}")
    assert ui.get_by_test_id("viewer-image").get_attribute("src") == frames[2]["jpeg_url"]
    assert "i=2" in ui.url

    # Paging keeps the select in sync.
    ui.get_by_test_id("viewer-next").click()
    expect(select).to_have_value("1")

    ui.get_by_test_id("viewer-mode").click()  # live: no frame selection
    expect(select).to_be_hidden()


def test_viewer_zoom_buttons_keys_and_pan(ui, live_server):
    live_server.ensure_frames("wien", 2)
    ui.reload()
    ui.get_by_test_id("preview-wien").click()
    expect(ui.get_by_test_id("viewer")).to_be_visible()
    _image_loaded(ui, "viewer-image")
    expect(ui.get_by_test_id("viewer-zoom-reset")).to_have_text("100 %")
    expect(ui.get_by_test_id("viewer-zoom-out")).to_be_disabled()

    ui.get_by_test_id("viewer-zoom-in").click()
    expect(ui.get_by_test_id("viewer-zoom-reset")).to_have_text("150 %")
    ui.get_by_test_id("viewer-zoom-in").click()
    ui.get_by_test_id("viewer-zoom-in").click()
    expect(ui.get_by_test_id("viewer-zoom-reset")).to_have_text("300 %")
    box = ui.get_by_test_id("viewer-image").bounding_box()
    assert box["width"] > ui.viewport_size["width"] * 2.5

    # Dragging with the mouse pans the zoomed image without hiding the bars.
    before = ui.get_by_test_id("viewer-image").evaluate("i => i.style.transform")
    vw, vh = ui.viewport_size["width"], ui.viewport_size["height"]
    ui.mouse.move(vw / 2, vh / 2)
    ui.mouse.down()
    ui.mouse.move(vw / 2 + 120, vh / 2 + 80, steps=5)
    ui.mouse.up()
    after = ui.get_by_test_id("viewer-image").evaluate("i => i.style.transform")
    assert after != before
    expect(ui.get_by_test_id("viewer")).not_to_have_class(re.compile("controls-hidden"))

    # Zoom is kept while paging.
    ui.get_by_test_id("viewer-prev").click()
    assert _zoom(ui) == 3

    ui.keyboard.press("-")
    expect(ui.get_by_test_id("viewer-zoom-reset")).to_have_text("200 %")
    ui.keyboard.press("0")
    expect(ui.get_by_test_id("viewer-zoom-reset")).to_have_text("100 %")
    _viewport_filled(ui)
    ui.keyboard.press("+")
    assert _zoom(ui) == 1.5
    ui.get_by_test_id("viewer-zoom-reset").click()
    assert _zoom(ui) == 1

    # Closing resets the zoom.
    ui.get_by_test_id("viewer-zoom-in").click()
    ui.keyboard.press("Escape")
    ui.get_by_test_id("preview-wien").click()
    expect(ui.get_by_test_id("viewer-zoom-reset")).to_have_text("100 %")


def test_timelapse_plays_through_history(ui, live_server):
    live_server.ensure_frames("wien", 3)
    ui.reload()
    ui.get_by_test_id("play-wien").click()
    expect(ui.get_by_test_id("viewer")).to_be_visible()
    expect(ui.get_by_test_id("viewer-play")).to_have_text("❚❚")
    seen = set()
    for _ in range(30):
        seen.add(ui.get_by_test_id("viewer-position").text_content())
        if len(seen) >= 3:
            break
        ui.wait_for_timeout(150)
    assert len(seen) >= 3, seen
    ui.keyboard.press(" ")
    expect(ui.get_by_test_id("viewer-play")).to_have_text("▶")


def test_live_stream_in_browser(ui, live_server):
    live_server.ensure_frames("wien", 1)
    ui.reload()
    ui.get_by_test_id("live-wien").click()
    expect(ui.get_by_test_id("viewer")).to_be_visible()
    expect(ui.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"/regions/wien/mjpeg"))
    _image_loaded(ui, "viewer-image")
    _viewport_filled(ui)
    expect(ui.get_by_test_id("viewer-prev")).to_be_hidden()
    assert "live=wien" in ui.url

    ui.get_by_test_id("viewer-mode").click()  # switch to history
    expect(ui.get_by_test_id("viewer-prev")).to_be_visible()
    expect(ui.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"/history/"))
    ui.get_by_test_id("viewer-close").click()
    expect(ui.get_by_test_id("viewer")).to_be_hidden()
    assert ui.get_by_test_id("viewer-image").get_attribute("src") is None


def test_viewer_format_switches_between_jpeg_and_png(ui, live_server):
    live_server.ensure_frames("wien", 2)
    ui.reload()
    ui.get_by_test_id("preview-wien").click()
    image = ui.get_by_test_id("viewer-image")
    fmt = ui.get_by_test_id("viewer-format")
    expect(fmt).to_be_visible()
    expect(fmt).to_have_value("jpg")
    expect(image).to_have_attribute("src", re.compile(r"/history/[^/]+\.jpg$"))
    _image_loaded(ui, "viewer-image")
    expect(ui.get_by_test_id("viewer-caption")).to_contain_text("JPEG")

    fmt.select_option("png")
    expect(image).to_have_attribute("src", re.compile(r"/history/[^/]+\.png$"))
    _image_loaded(ui, "viewer-image")
    _viewport_filled(ui)
    expect(ui.get_by_test_id("viewer-caption")).to_contain_text("PNG")
    assert "f=png" in ui.url
    ui.get_by_test_id("viewer-prev").click()  # paging keeps the format
    expect(image).to_have_attribute("src", re.compile(r"\.png$"))

    # The choice survives a reload.
    ui.keyboard.press("Escape")
    ui.reload()
    ui.get_by_test_id("preview-wien").click()
    expect(fmt).to_have_value("png")
    fmt.select_option("jpg")
    expect(image).to_have_attribute("src", re.compile(r"\.jpg$"))
    assert "f=" not in ui.url

    # The archive offers JPEG and PNG as well (both are pre-rendered).
    ui.keyboard.press("Escape")
    ui.get_by_test_id("archive-wien").click()
    expect(fmt).to_be_visible()
    expect(fmt).to_have_value("jpg")
    assert [o.strip() for o in fmt.locator("option").all_inner_texts()][:2]


@pytest.mark.parametrize("page_fixture", ["ui", "mobile_page"])
def test_live_format_mp4_and_gif(request, page_fixture, live_server):
    ui = request.getfixturevalue(page_fixture)
    live_server.ensure_frames("wien", 2)
    ui.goto(live_server.url + "/")
    ui.get_by_test_id("live-wien").click()
    fmt = ui.get_by_test_id("viewer-format")
    expect(fmt).to_have_value("mjpeg")
    expect(fmt.locator("option")).to_have_count(3)

    fmt.select_option("gif")
    expect(ui.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"/regions/wien/animation\.gif"))
    _image_loaded(ui, "viewer-image")
    expect(ui.get_by_test_id("viewer-caption")).to_contain_text("GIF")
    assert "f=gif" in ui.url

    fmt.select_option("mp4")
    video = ui.get_by_test_id("viewer-video")
    expect(video).to_be_visible()
    expect(ui.get_by_test_id("viewer-image")).to_be_hidden()
    expect(video).to_have_attribute("src", re.compile(r"/regions/wien/animation\.mp4"))
    ui.wait_for_function(
        "() => document.querySelector('[data-testid=\"viewer-video\"]').readyState >= 1", timeout=30000
    )
    _viewport_filled(ui, "viewer-video")
    ui.get_by_test_id("viewer-zoom-in").click()
    assert "scale(1.5)" in video.evaluate("v => v.style.transform")
    ui.get_by_test_id("viewer-zoom-reset").click()

    # Back to history: video stopped, image visible again.
    ui.get_by_test_id("viewer-mode").click()
    expect(video).to_be_hidden()
    expect(ui.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"/history/"))
    assert video.get_attribute("src") is None
    ui.get_by_test_id("viewer-mode").click()  # live remembers MP4
    expect(fmt).to_have_value("mp4")
    fmt.select_option("mjpeg")
    expect(ui.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"/regions/wien/mjpeg"))
    ui.get_by_test_id("viewer-close").click()
    assert video.get_attribute("src") is None


def test_live_deep_link_opens_stream(page, live_server):
    live_server.ensure_frames("mallorca", 1)
    page.goto(live_server.url + "/live/mallorca")
    expect(page.get_by_test_id("viewer")).to_be_visible()
    expect(page.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"/regions/mallorca/mjpeg"))
    _image_loaded(page, "viewer-image")


@pytest.fixture
def mobile_page(browser, live_server):
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True
    )
    context.add_init_script(_OPEN_FOLDS)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    yield page
    context.close()
    assert errors == [], f"JavaScript errors in the UI: {errors}"


def test_mobile_viewer_fills_screen_and_back_closes(mobile_page, live_server):
    live_server.ensure_frames("wien", 2)
    page = mobile_page
    page.goto(live_server.url + "/")
    # No horizontal scrolling on a phone.
    page.wait_for_load_state("load")
    page.wait_for_timeout(500)  # content loaded via JS
    width = page.evaluate("document.documentElement.scrollWidth")
    offenders = page.evaluate(
        "() => [...document.querySelectorAll('body *')].filter(e => {"
        " const r = e.getBoundingClientRect(); if (r.right <= 391) return false;"
        " for (let p = e.parentElement; p; p = p.parentElement) {"
        "   const o = getComputedStyle(p).overflowX; if (o === 'auto' || o === 'hidden') return false; }"
        " return true; }).slice(0, 5).map(e => e.outerHTML.slice(0, 120))"
    )
    assert width <= 390, offenders

    page.get_by_test_id("preview-wien").tap()
    expect(page.get_by_test_id("viewer")).to_be_visible()
    _image_loaded(page, "viewer-image")
    _viewport_filled(page)
    expect(page.get_by_test_id("viewer-prev")).to_be_visible()
    expect(page.get_by_test_id("viewer-close")).to_be_in_viewport()

    page.get_by_test_id("viewer-prev").tap()
    expect(page.get_by_test_id("viewer-position")).to_contain_text("/")

    # Frame selection and zoom are reachable and usable on a phone.
    for testid in ("viewer-frame", "viewer-zoom-in", "viewer-zoom-out", "viewer-zoom-reset"):
        expect(page.get_by_test_id(testid)).to_be_in_viewport()
    page.get_by_test_id("viewer-frame").select_option("0")
    expect(page.get_by_test_id("viewer-caption")).to_contain_text("latest image")
    page.get_by_test_id("viewer-zoom-in").tap()
    expect(page.get_by_test_id("viewer-zoom-reset")).to_have_text("150 %")
    page.get_by_test_id("viewer-zoom-reset").tap()
    expect(page.get_by_test_id("viewer-zoom-reset")).to_have_text("100 %")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390

    # Format selection reachable on a phone; PNG loads and fills the screen.
    expect(page.get_by_test_id("viewer-format")).to_be_in_viewport()
    page.get_by_test_id("viewer-format").select_option("png")
    expect(page.get_by_test_id("viewer-image")).to_have_attribute("src", re.compile(r"\.png$"))
    _image_loaded(page, "viewer-image")
    _viewport_filled(page)
    page.get_by_test_id("viewer-format").select_option("jpg")

    # The phone's back button closes the viewer but stays on the page.
    page.go_back()
    expect(page.get_by_test_id("viewer")).to_be_hidden()
    assert page.url.rstrip("/").split("#")[0] == live_server.url


# --- Logs ----------------------------------------------------------------

def test_logs_are_shown_at_the_bottom(ui, live_server):
    live_server.ensure_frames("wien", 1)
    last_two = ui.evaluate(
        "() => [...document.querySelectorAll('main > section')].slice(-2).map((s) => s.id)"
    )
    # Logs are at the bottom, followed only by the API link list.
    assert last_two == ["sec-logs", "sec-api"]
    logs = ui.get_by_test_id("logs")
    expect(logs.locator('[data-testid="log-line"]').first).to_be_visible(timeout=10000)

    ui.get_by_test_id("refresh-mallorca").click()
    expect(logs).to_contain_text("Manual refresh requested for mallorca", timeout=10000)

    ui.get_by_test_id("logs-level").select_option("ERROR")
    expect(ui.locator('[data-testid="log-line"][data-level="INFO"]').first).to_be_hidden()
    ui.get_by_test_id("logs-clear").click()
    expect(ui.locator('[data-testid="log-line"]')).to_have_count(0)


# --- Sources -------------------------------------------------------------

def test_sources_json_editor_is_collapsed_and_editable(ui, live_server):
    editor = ui.get_by_test_id("sources-json")
    expect(editor).to_be_hidden()
    expect(ui.get_by_test_id("sources-json-details")).not_to_have_attribute("open", "")

    ui.get_by_test_id("sources-json-details").locator("summary").click()
    expect(editor).to_be_visible()
    expect(editor).to_have_value(re.compile(r'"msg_seviri"'))

    editor.fill("[broken")
    ui.get_by_test_id("sources-json-save").click()
    expect(ui.get_by_test_id("sources-json-message")).to_contain_text("Invalid JSON")

    catalog = _stored_catalog_or_default(ui)
    # Driver dummy: the region must not trigger a real download afterwards.
    catalog.append({"id": "iodc", "driver": "dummy", "label": "MSG Indian Ocean",
                    "collection": "EO:EUM:DAT:MSG:HRSEVIRI-IODC", "enabled": True})
    editor.fill(json.dumps(catalog, indent=2))
    ui.get_by_test_id("sources-json-save").click()
    expect(ui.get_by_test_id("sources-json-message")).to_have_text("Source catalog saved.")
    expect(ui.get_by_test_id("source-row-iodc")).to_be_visible()
    assert "iodc" in [e["id"] for e in _stored(live_server)["sources"]["catalog"]]

    # Server-side validation: regions still reference dummy.
    editor.fill(json.dumps([e for e in catalog if e["id"] != "dummy"]))
    ui.get_by_test_id("sources-json-save").click()
    expect(ui.get_by_test_id("sources-json-message")).to_contain_text("Saving failed")

    # The new source is immediately selectable for the region.
    ui.get_by_test_id("region-source-wien").select_option("iodc")
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Source saved")
    regions = {r["name"]: r for r in _stored(live_server)["regions"]}
    assert regions["wien"]["source"] == "iodc"


def _stored_catalog_or_default(page) -> list:
    return page.evaluate("fetch('/api/sources').then(r => r.json()).then(d => d.catalog)")


def test_sources_show_cycle_and_custom_cycle_via_json(ui, live_server):
    expect(ui.get_by_test_id("source-cycle-msg_seviri")).to_contain_text("Cycle 5 min (automatic)")
    expect(ui.get_by_test_id("source-cycle-msg_seviri_0deg")).to_contain_text("Cycle 15 min")
    expect(ui.get_by_test_id("source-cycle-mtg_fci")).to_contain_text("Cycle 10 min")
    expect(ui.get_by_test_id("source-cycle-dummy")).to_contain_text("no download")

    catalog = _stored_catalog_or_default(ui)
    for entry in catalog:
        if entry["id"] == "mtg_fci":
            entry["cycle_minutes"] = 20
    ui.get_by_test_id("sources-json-details").locator("summary").click()
    ui.get_by_test_id("sources-json").fill(json.dumps(catalog))
    ui.get_by_test_id("sources-json-save").click()
    expect(ui.get_by_test_id("sources-json-message")).to_have_text("Source catalog saved.")
    expect(ui.get_by_test_id("source-cycle-mtg_fci")).to_contain_text("Cycle 20 min")
    expect(ui.get_by_test_id("source-cycle-mtg_fci")).not_to_contain_text("automatic")
    stored = {e["id"]: e for e in _stored(live_server)["sources"]["catalog"]}
    assert stored["mtg_fci"]["cycle_minutes"] == 20

    # Sources without a fixed cycle follow the general polling interval.
    ui.get_by_test_id("poll-interval").fill("7")
    ui.get_by_test_id("save-sources").click()
    expect(ui.get_by_test_id("source-cycle-dummy")).to_contain_text("Cycle 7 min")


def test_sources_cycle_on_mobile(mobile_page, live_server):
    mobile_page.goto(live_server.url + "/#sec-sources")
    expect(mobile_page.get_by_test_id("source-cycle-msg_seviri")).to_be_visible()
    expect(mobile_page.get_by_test_id("source-cycle-msg_seviri")).to_contain_text("Cycle 5 min")
    width = mobile_page.evaluate("() => document.documentElement.scrollWidth")
    assert width <= 390


def test_toggle_source_and_region_warning(ui, live_server):
    expect(ui.get_by_test_id("region-warning-wien")).to_be_hidden()
    ui.get_by_test_id("source-enabled-dummy").uncheck()
    expect(ui.get_by_test_id("sources-table-message")).to_have_text("Source dummy disabled.")
    expect(ui.get_by_test_id("region-warning-wien")).to_be_visible()
    catalog = {e["id"]: e for e in _stored(live_server)["sources"]["catalog"]}
    assert catalog["dummy"]["enabled"] is False

    ui.get_by_test_id("source-enabled-dummy").check()
    expect(ui.get_by_test_id("sources-table-message")).to_have_text("Source dummy enabled.")
    expect(ui.get_by_test_id("region-warning-wien")).to_be_hidden()


def test_sync_with_eumetsat_and_adopt(page, start_server, fake_eumetsat):
    server = start_server(env={"HA_SATIMAGE_EUMETSAT_API": fake_eumetsat})
    page.goto(server.url + "/")
    expect(page.get_by_test_id("sources-last-sync")).to_have_text("never")
    page.get_by_test_id("sources-sync").click()
    expect(page.get_by_test_id("sources-sync-message")).to_have_text("Sync completed.")
    expect(page.get_by_test_id("source-sync-status-msg_seviri")).to_contain_text("available")
    expect(page.get_by_test_id("discovered-EO-EUM-DAT-MSG-HRSEVIRI-IODC")).to_be_visible()

    page.get_by_test_id("adopt-EO-EUM-DAT-MSG-HRSEVIRI-IODC").click()
    expect(page.get_by_test_id("sources-sync-message")).to_contain_text("adopted")
    expect(page.get_by_test_id("discovered-EO-EUM-DAT-MSG-HRSEVIRI-IODC")).to_have_count(0)
    catalog = {e["collection"]: e for e in _stored(server)["sources"]["catalog"]}
    assert catalog["EO:EUM:DAT:MSG:HRSEVIRI-IODC"]["enabled"] is False


def _server_in_maintenance(start_server, fake_maintenance):
    """Wien on Rapid Scan while the fake RSS schedule announces maintenance."""
    server = start_server(env={"HA_SATIMAGE_MAINTENANCE_URL": fake_maintenance})
    config = httpx.get(server.url + "/api/config").json()
    for entry in config["sources"]["catalog"]:
        if entry["id"] == "msg_seviri":
            entry["enabled"] = True
    for region in config["regions"]:
        if region["name"] == "wien":
            region["source"] = "msg_seviri"
    response = httpx.post(server.url + "/api/config", json={
        "sources": {"catalog": config["sources"]["catalog"]}, "regions": config["regions"],
    })
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        frames = server.frames("wien")
        if frames and frames[0]["composite"] == "maintenance":
            return server
        time.sleep(0.2)
    raise AssertionError("No maintenance frame for wien")


def test_maintenance_window_pauses_source_and_shows_image(page, start_server, fake_maintenance):
    server = _server_in_maintenance(start_server, fake_maintenance)
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(server.url + "/#sec-sources")
    badge = page.get_by_test_id("source-maintenance-msg_seviri")
    expect(badge).to_be_visible()
    expect(badge).to_contain_text("Maintenance, queries paused")
    expect(badge).to_contain_text("next check")
    expect(page.get_by_test_id("source-download-error-msg_seviri")).to_have_count(0)
    _image_loaded(page, "preview-wien")
    # No Data Store query (and so no warning/error) while in maintenance.
    logs = httpx.get(server.url + "/api/logs?format=text").text
    assert "scheduled maintenance" in logs
    assert "Download msg_seviri failed" not in logs

    page.get_by_test_id("maintenance-check-hours").fill("12")
    page.get_by_test_id("save-sources").click()
    expect(page.get_by_test_id("sources-message")).to_have_text("Settings saved.")
    assert _stored(server)["sources"]["maintenance_check_hours"] == 12
    # 0 switches the check off: the source no longer shows the window.
    page.get_by_test_id("maintenance-check-hours").fill("0")
    page.get_by_test_id("save-sources").click()
    expect(page.get_by_test_id("source-maintenance-msg_seviri")).to_have_count(0)
    assert errors == []


def test_maintenance_window_on_mobile(browser, start_server, fake_maintenance):
    server = _server_in_maintenance(start_server, fake_maintenance)
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True
    )
    try:
        page = context.new_page()
        page.goto(server.url + "/#sec-sources")
        expect(page.get_by_test_id("source-maintenance-msg_seviri")).to_be_visible()
        expect(page.get_by_test_id("maintenance-check-hours")).to_be_visible()
        _image_loaded(page, "preview-wien")
        width = page.evaluate("() => document.documentElement.scrollWidth")
        assert width <= 390
    finally:
        context.close()


def test_sync_failure_is_shown(ui):
    ui.get_by_test_id("sources-sync").click()
    expect(ui.get_by_test_id("sources-sync-message")).to_contain_text("Sync failed")


# --- Storage location ---------------------------------------------------------

def test_change_storage_location_via_ui(page, live_server):
    live_server.ensure_frames("wien", 2)
    (live_server.storage_root / "data").mkdir()
    target = live_server.storage_root / "data" / "ha_satimage"
    page.goto(live_server.url + "/")
    expect(page.get_by_test_id("storage-current")).to_have_text(str(live_server.data_dir / "frames"))

    candidate = page.locator(f'[data-testid^="storage-candidate-"][data-path="{target}"]')
    expect(candidate).to_be_visible()
    candidate.click()
    expect(page.get_by_test_id("storage-path")).to_have_value(str(target))
    page.get_by_test_id("history-minutes").fill("180")
    page.get_by_test_id("save-storage").click()

    expect(page.get_by_test_id("storage-message")).to_contain_text(f"Storage location: {target}")
    expect(page.get_by_test_id("storage-current")).to_have_text(str(target))
    assert list((target / "wien").glob("*.png"))
    stored = _stored(live_server)
    assert stored["storage"]["frames_dir"] == str(target)
    assert stored["history"]["history_minutes"] == 180
    _image_loaded(page, "preview-wien")
    expect(page.get_by_test_id("history-item-wien-0")).to_be_visible()


def test_invalid_storage_location_shows_error(ui):
    ui.get_by_test_id("storage-path").fill("relative/path")
    ui.get_by_test_id("save-storage").click()
    message = ui.get_by_test_id("storage-message")
    expect(message).to_have_class(re.compile(r"\berror\b"))
    expect(message).to_contain_text("Saving failed")


# --- Switching source/image type --------------------------------------------------

def test_change_composite_rerenders_and_updates_preview(ui, live_server):
    live_server.ensure_frames("wien", 1)
    before = {f["filename"] for f in live_server.frames("wien")}
    select = ui.get_by_test_id("region-composite-wien")
    expect(select).to_have_value("natural_color_hrv_with_night_ir")

    select.select_option("natural_color_hrv")
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Image type saved")
    regions = {r["name"]: r for r in _stored(live_server)["regions"]}
    assert regions["wien"]["composite"] == "natural_color_hrv"

    # Without another click: the server re-renders, the history reloads.
    expect(ui.get_by_test_id("history-item-wien-0")).to_be_visible()
    ui.wait_for_function(
        "(n) => document.querySelectorAll('[data-testid^=\"history-item-wien-\"]').length > n",
        arg=len(before), timeout=15000,
    )
    latest = live_server.frames("wien")[0]
    assert latest["filename"] not in before
    assert latest["composite"] == "natural_color_hrv"


def test_toggle_borders_rerenders(ui, live_server):
    live_server.ensure_frames("wien", 1)
    from ha_satimage.archive import RenderArchive, region_signature
    from ha_satimage.config import RegionConfig

    archive = RenderArchive(live_server.data_dir / "frames" / "_renders")
    wien = next(
        region for region in httpx.get(f"{live_server.url}/api/config").json()["regions"]
        if region["name"] == "wien"
    )
    region = RegionConfig(**wien)
    archive.sync_region("wien", region_signature(region))
    sensing = datetime.now(timezone.utc) - timedelta(minutes=1)
    original = archive.store(
        "wien", "dummy", "cloudtop", sensing,
        httpx.get(f"{live_server.url}/regions/wien/latest.png").content,
    )
    assert original is not None
    # The archive is only (re-)rendered with "download all sources".
    assert httpx.post(f"{live_server.url}/api/storage", json={"archive_render_all": True}).status_code == 200

    def wait_for_archive_rerender(previous_path) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not previous_path.exists() and archive.images("wien", "dummy", "cloudtop"):
                return
            time.sleep(0.1)
        raise AssertionError("Archived images were not regenerated after changing borders")

    before = {f["filename"] for f in live_server.frames("wien")}
    box = ui.get_by_test_id("region-borders-wien")
    expect(box).to_be_checked()

    box.uncheck()
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Country borders disabled")
    regions = {r["name"]: r for r in _stored(live_server)["regions"]}
    assert regions["wien"]["borders"] is False
    assert regions["mallorca"]["borders"] is True
    wait_for_archive_rerender(original.path)

    ui.wait_for_function(
        "(n) => document.querySelectorAll('[data-testid^=\"history-item-wien-\"]').length > n",
        arg=len(before), timeout=15000,
    )
    latest = live_server.frames("wien")[0]
    assert latest["filename"] not in before
    assert latest["borders"] is False

    previous_path = archive.images("wien", "dummy", "cloudtop")[0].path
    previous_png = previous_path.read_bytes()
    box.check()
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Country borders enabled")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and previous_path.read_bytes() == previous_png:
        time.sleep(0.1)
    assert previous_path.exists()
    assert previous_path.read_bytes() != previous_png

    ui.reload()
    expect(ui.get_by_test_id("region-borders-wien")).to_be_checked()


def test_borders_checkbox_on_mobile(mobile_page, live_server):
    mobile_page.goto(live_server.url + "/")
    box = mobile_page.get_by_test_id("region-borders-wien")
    expect(box).to_be_visible()
    box.tap()
    expect(mobile_page.get_by_test_id("refresh-message-wien")).to_contain_text("Country borders disabled")


@pytest.mark.parametrize("page_fixture", ["ui", "mobile_page"])
def test_enabling_borders_keeps_island_archive_history(request, page_fixture, live_server):
    import io

    from PIL import Image

    from ha_satimage.archive import RenderArchive, region_signature
    from ha_satimage.config import RegionConfig

    page = request.getfixturevalue(page_fixture)
    page.goto(live_server.url + "/")
    box = page.get_by_test_id("region-borders-mallorca")
    box.uncheck()
    expect(page.get_by_test_id("refresh-message-mallorca")).to_contain_text("Country borders disabled")
    region = RegionConfig(**next(
        r for r in _stored(live_server)["regions"] if r["name"] == "mallorca"
    ))
    archive = RenderArchive(live_server.data_dir / "frames" / "_renders")
    marker = archive.root / "mallorca" / "region.json"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text() == region_signature(region):
            break
        time.sleep(0.1)
    assert marker.read_text() == region_signature(region)
    out = io.BytesIO()
    Image.new("RGB", (300, 300), "white").save(out, format="PNG")
    original = out.getvalue()
    captures = [datetime.now(timezone.utc) - timedelta(minutes=m) for m in (10, 20, 30)]
    for capture in captures:
        archive.store("mallorca", "dummy", "cloudtop", capture, original)
    old_slots = {image.slot for image in archive.images("mallorca", "dummy", "cloudtop")}
    box.check()
    expect(page.get_by_test_id("refresh-message-mallorca")).to_contain_text("Country borders enabled")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if marker.read_text() == region_signature(region.model_copy(update={"borders": True})):
            break
        time.sleep(0.1)
    images = archive.images("mallorca", "dummy", "cloudtop")
    assert old_slots <= {image.slot for image in images}
    for image in images:
        if image.slot in old_slots:
            assert Image.open(image.path).getextrema()[0][0] < 100
            assert Image.open(image.jpeg_path).getextrema()[0][0] < 100
    page.get_by_test_id("archive-mallorca").click()
    page.get_by_test_id("viewer-composite").select_option("cloudtop")
    _image_loaded(page, "viewer-image")
    page.get_by_test_id("viewer-format").select_option("png")
    _image_loaded(page, "viewer-image")
    page.get_by_test_id("viewer-prev").click()
    _image_loaded(page, "viewer-image")


def test_placeholder_source_is_marked(ui, live_server):
    expect(ui.get_by_test_id("region-placeholder-wien")).to_be_visible()  # test server: dummy
    options = ui.get_by_test_id("region-source-wien").locator("option")
    expect(options.filter(has_text="Data Tailor")).to_contain_text("placeholder")
    expect(options.filter(has_text="Rapid Scan")).not_to_contain_text("placeholder")
    expect(ui.get_by_test_id("source-row-data_tailor")).to_contain_text("placeholder")

    ui.get_by_test_id("region-source-wien").select_option("msg_seviri")
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Source saved")
    expect(ui.get_by_test_id("region-placeholder-wien")).to_be_hidden()


# --- API-Links ----------------------------------------------------------------

def test_api_links_follow_selected_region_and_work(ui, live_server):
    live_server.ensure_frames("mallorca", 1)
    latest = ui.get_by_test_id("api-link-latest")
    expect(latest).to_have_attribute("href", "/regions/wien/latest.png")
    expect(latest).to_have_text(live_server.url + "/regions/wien/latest.png")

    ui.get_by_test_id("api-region").select_option("mallorca")
    expect(latest).to_have_attribute("href", "/regions/mallorca/latest.png")
    expect(ui.get_by_test_id("api-link-mjpeg")).to_have_attribute("href", "/regions/mallorca/mjpeg")
    expect(ui.get_by_test_id("api-link-live")).to_have_attribute("href", "/live/mallorca")
    expect(ui.get_by_test_id("api-post-1")).to_contain_text("/api/regions/mallorca/refresh")
    # General links do not depend on the location.
    expect(ui.get_by_test_id("api-link-status")).to_have_attribute("href", "/api/status")

    # Clicking opens a new tab, the UI stays put.
    with ui.context.expect_page() as new_tab:
        latest.click()
    tab = new_tab.value
    tab.wait_for_load_state()
    assert tab.url == live_server.url + "/regions/mallorca/latest.png"
    assert tab.evaluate("() => document.images[0] && document.images[0].naturalWidth") > 0
    tab.close()
    assert ui.url.startswith(live_server.url + "/")

    # All read links respond (except MJPEG/MP4: stream or optional).
    for link in ui.locator("[data-testid^='api-link-']").all():
        testid = link.get_attribute("data-testid")
        if testid in ("api-link-mjpeg", "api-link-mp4"):
            continue
        href = link.get_attribute("href")
        response = ui.request.get(live_server.url + href, max_redirects=0)
        assert response.status in (200, 307), (href, response.status)


def test_api_links_on_mobile_do_not_overflow(mobile_page, live_server):
    mobile_page.goto(live_server.url + "/#sec-api")
    expect(mobile_page.get_by_test_id("api-link-latest")).to_be_visible()
    width = mobile_page.evaluate("() => document.documentElement.scrollWidth")
    assert width <= 390


# --- MTG FCI: image types, raw data archive ----------------------------------------

def _png(color) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(out, format="PNG")
    return out.getvalue()


def test_switching_to_fci_offers_fci_composites(ui, live_server):
    select = ui.get_by_test_id("region-composite-wien")
    expect(select).to_have_value("natural_color_hrv_with_night_ir")

    ui.get_by_test_id("region-source-wien").select_option("mtg_fci")
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Source and image type saved")
    expect(select).to_have_value("natural_color_with_night_cloudtop")
    expect(select.locator("option", has_text="HRV")).to_have_count(0)
    region = {r["name"]: r for r in _stored(live_server)["regions"]}["wien"]
    assert (region["source"], region["composite"]) == ("mtg_fci", "natural_color_with_night_cloudtop")
    expect(ui.get_by_test_id("region-placeholder-wien")).to_be_hidden()

    # "natural_color" exists for both satellites -> kept when switching back.
    select.select_option("natural_color")
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Image type saved")
    ui.get_by_test_id("region-source-wien").select_option("msg_seviri")
    expect(ui.get_by_test_id("refresh-message-wien")).to_contain_text("Source saved")
    expect(select).to_have_value("natural_color")
    expect(select.locator("option", has_text="HRV").first).to_be_attached()


def _add_hrfi_source(server):
    """FCI High Resolution (HRFI) catalog entry, disabled (no downloads in tests)."""
    config = httpx.get(server.url + "/api/config").json()
    catalog = [*config["sources"]["catalog"], {
        "id": "mtg_fci_0665", "driver": "mtg_fci", "label": "FCI HRFI",
        "collection": "EO:EUM:DAT:0665", "enabled": False,
    }]
    response = httpx.post(server.url + "/api/config", json={"sources": {"catalog": catalog}})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("phone", [False, True])
def test_switching_to_fci_hrfi_offers_only_hrfi_composites(page, mobile_page, live_server, phone):
    _add_hrfi_source(live_server)
    view = mobile_page if phone else page
    view.goto(live_server.url + "/")
    select = view.get_by_test_id("region-composite-wien")
    view.get_by_test_id("region-source-wien").select_option("mtg_fci_0665")
    expect(view.get_by_test_id("refresh-message-wien")).to_contain_text("Source and image type saved")
    expect(select).to_have_value("ir_sandwich_with_night_colorized_ir_clouds")
    values = select.locator("option").evaluate_all("(options) => options.map((o) => o.value)")
    assert set(values) == {
        "ir_sandwich_with_night_colorized_ir_clouds", "ir_sandwich", "hrv_clouds", "colorized_ir_clouds",
    }
    region = {r["name"]: r for r in _stored(live_server)["regions"]}["wien"]
    assert (region["source"], region["composite"]) == (
        "mtg_fci_0665", "ir_sandwich_with_night_colorized_ir_clouds",
    )

    # After a reload the server-side list is the same.
    view.reload()
    select = view.get_by_test_id("region-composite-wien")
    expect(select).to_have_value("ir_sandwich_with_night_colorized_ir_clouds")
    expect(select.locator("option")).to_have_count(4)

    # Back to the normal FCI collection: natural color is offered again.
    view.get_by_test_id("region-source-wien").select_option("mtg_fci")
    expect(select).to_have_value("natural_color_with_night_cloudtop")
    expect(select.locator("option[value='natural_color']")).to_have_count(1)


def _archive_images(live_server, region="wien", source="dummy",
                    composite="cloudtop", colors=("red", "green")):
    """Put pre-rendered images into the archive (as the scheduler would)."""
    from datetime import datetime, timedelta, timezone

    from ha_satimage.archive import RenderArchive

    archive = RenderArchive(live_server.data_dir / "frames" / "_renders")
    base = datetime(2026, 9, 26, 14, 10, tzinfo=timezone.utc)
    for index, color in enumerate(colors):
        archive.store(region, source, composite, base + timedelta(minutes=10 * index), _png(color))
    return archive


def test_archive_viewer_browses_sources_and_image_types(ui, live_server):
    archive = _archive_images(live_server)
    archive.store("wien", "dummy", "airmass", __import__("datetime").datetime(
        2026, 9, 26, 14, 20, tzinfo=__import__("datetime").timezone.utc), _png("blue"))
    ui.reload()

    ui.get_by_test_id("archive-wien").click()
    viewer = ui.get_by_test_id("viewer")
    expect(viewer).to_be_visible()
    image = ui.get_by_test_id("viewer-image")
    ui.get_by_test_id("viewer-composite").select_option("cloudtop")
    expect(ui.get_by_test_id("viewer-position")).to_have_text("2 / 2")
    expect(image).to_have_attribute("src", re.compile(r"/archive/dummy/cloudtop/20260926T142000Z\.jpg"))
    _image_loaded(ui, "viewer-image")
    expect(ui.get_by_test_id("viewer-caption")).to_contain_text("Cloud tops")
    # Pre-rendered images can be played back like the normal history.
    expect(ui.get_by_test_id("viewer-play")).to_be_visible()
    expect(ui.get_by_test_id("viewer-loading")).to_be_hidden()
    assert "archive=wien" in ui.url

    ui.get_by_test_id("viewer-prev").click()
    expect(ui.get_by_test_id("viewer-position")).to_have_text("1 / 2")
    expect(image).to_have_attribute("src", re.compile(r"20260926T141000Z"))

    # The source can be chosen in the archive.
    source = ui.get_by_test_id("viewer-source")
    expect(source).to_be_visible()
    expect(source).to_have_value("dummy")

    # ... and so can the image type.
    ui.get_by_test_id("viewer-composite").select_option("airmass")
    expect(image).to_have_attribute("src", re.compile(r"/archive/dummy/airmass/20260926T142000Z"))
    _image_loaded(ui, "viewer-image")
    assert "c=airmass" in ui.url

    # Format selection works here too (both are pre-rendered).
    ui.get_by_test_id("viewer-format").select_option("png")
    expect(image).to_have_attribute("src", re.compile(r"\.png\?v=\d+$"))
    _image_loaded(ui, "viewer-image")

    ui.get_by_test_id("viewer-mode").click()  # back to the normal history
    expect(ui.get_by_test_id("viewer-source")).to_be_hidden()
    expect(ui.get_by_test_id("viewer-composite")).to_be_hidden()
    ui.get_by_test_id("viewer-close").click()
    expect(viewer).to_be_hidden()


def test_archive_viewer_on_the_phone(mobile_page, live_server):
    _archive_images(live_server)
    mobile_page.goto(live_server.url + "/")
    mobile_page.get_by_test_id("archive-wien").tap()
    expect(mobile_page.get_by_test_id("viewer")).to_be_visible()
    expect(mobile_page.get_by_test_id("viewer-source")).to_be_visible()
    _image_loaded(mobile_page, "viewer-image")
    expect(mobile_page.get_by_test_id("viewer-position")).to_have_text("2 / 2")
    mobile_page.get_by_test_id("viewer-prev").tap()
    expect(mobile_page.get_by_test_id("viewer-position")).to_have_text("1 / 2")
    assert mobile_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    mobile_page.go_back()
    expect(mobile_page.get_by_test_id("viewer")).to_be_hidden()


@pytest.mark.parametrize("page_fixture", ["page", "mobile_page"])
def test_fci_archive_shows_only_available_images_without_rendering(request, page_fixture, fci_server):
    from ha_satimage.archive import RenderArchive
    from ha_satimage.config import DEFAULT_FCI_COMPOSITE

    page = request.getfixturevalue(page_fixture)
    url, calls, frames, names = fci_server
    archive = RenderArchive(frames / "_renders")
    archive.store("wien", "dummy", "cloudtop", datetime(2026, 9, 26, tzinfo=timezone.utc), _png("blue"))
    # Only the newest raw slot was rendered after its download; the older one was not.
    newest = datetime.strptime(names[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    archive.store("wien", "mtg_fci", DEFAULT_FCI_COMPOSITE, newest, _png("green"))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url + "/")
    page.get_by_test_id("archive-wien").click()
    page.get_by_test_id("viewer-source").select_option("mtg_fci")
    image = page.get_by_test_id("viewer-image")
    expect(image).to_have_attribute("src", re.compile(names[1]))
    _image_loaded(page, "viewer-image")
    expect(page.get_by_test_id("viewer-position")).to_have_text("1 / 1")
    expect(page.get_by_test_id("viewer-loading")).to_be_hidden()
    for ext in ("jpg", "png"):
        page.get_by_test_id("viewer-format").select_option(ext)
        expect(image).to_have_attribute("src", re.compile(rf"{names[1]}\.{ext}"))
        _image_loaded(page, "viewer-image")
    page.reload()  # Deep link restores the same source/composite.
    expect(page.get_by_test_id("viewer-source")).to_have_value("mtg_fci")
    _image_loaded(page, "viewer-image")
    listing = httpx.get(f"{url}/api/regions/wien/archive", params={
        "source": "mtg_fci", "composite": DEFAULT_FCI_COMPOSITE,
    }).json()
    assert [item["name"] for item in listing["images"]] == [names[1]]
    assert calls == []
    assert errors == []


def test_archive_deep_link_restores_source_and_image_type(ui, live_server):
    _archive_images(live_server, composite="airmass")
    ui.goto(ui.url.split("#")[0] + "#archive=wien&s=dummy&c=airmass&f=png")
    expect(ui.get_by_test_id("viewer")).to_be_visible()
    expect(ui.get_by_test_id("viewer-composite")).to_have_value("airmass")
    expect(ui.get_by_test_id("viewer-format")).to_have_value("png")
    expect(ui.get_by_test_id("viewer-image")).to_have_attribute(
        "src", re.compile(r"/archive/dummy/airmass/\d{8}T\d{6}Z\.png")
    )
    _image_loaded(ui, "viewer-image")


def test_archive_viewer_without_data_explains_why(ui):
    ui.get_by_test_id("archive-mallorca").click()
    expect(ui.get_by_test_id("viewer")).to_contain_text("Nothing archived yet")


def test_archive_settings_are_saved(ui, live_server):
    expect(ui.get_by_test_id("archive-summary")).to_contain_text("No MTG FCI source active")
    expect(ui.get_by_test_id("archive-summary")).to_contain_text("32–40")
    expect(ui.get_by_test_id("renders-summary")).to_contain_text("Off")
    ui.get_by_test_id("archive-retention").fill("6")
    ui.get_by_test_id("archive-chunk-min").fill("30")
    ui.get_by_test_id("archive-render-all").check()
    ui.get_by_test_id("archive-render-retention").fill("8")
    ui.get_by_test_id("archive-render-storage").fill("500")
    ui.get_by_test_id("save-storage").click()
    expect(ui.get_by_test_id("storage-message")).to_contain_text("Saved")
    assert _stored(live_server)["archive"] == {
        "retention_hours": 6, "chunk_min": 30, "chunk_max": 40,
        "render_all": True, "render_retention_hours": 8, "render_max_storage_mb": 500,
    }
    expect(ui.get_by_test_id("archive-summary")).to_contain_text("30–40")
    expect(ui.get_by_test_id("renders-summary")).to_contain_text("Active for")
    expect(ui.get_by_test_id("renders-summary")).to_contain_text("8 h")


@pytest.mark.parametrize("page_fixture", ["ui", "mobile_page"])
def test_clear_archive_confirms_deletes_and_preserves_history(request, page_fixture, live_server):
    import json

    page = request.getfixturevalue(page_fixture)
    archive = _archive_images(live_server)
    raw = live_server.data_dir / "frames" / "_archive" / "EO_EUM_DAT_0662" / "20260926T142000Z"
    raw.mkdir(parents=True)
    (raw / "chunk_0036.nc").write_bytes(b"raw")
    (raw / "meta.json").write_text(json.dumps({
        "product_id": "test", "sensing_start": "2026-09-26T14:20:00+00:00",
        "sensing_end": "2026-09-26T14:20:00+00:00",
    }))
    frames = live_server.ensure_frames("wien", 1)
    page.goto(live_server.url + "/")
    page.get_by_test_id("sec-storage-fold").evaluate("(el) => { el.open = true; }")
    button = page.get_by_test_id("archive-clear")
    expect(button).to_be_visible()
    page.once("dialog", lambda dialog: dialog.dismiss())
    button.click()
    assert archive.count() == 2
    assert raw.exists()
    page.once("dialog", lambda dialog: dialog.accept())
    button.click()
    expect(page.get_by_test_id("archive-clear-message")).to_contain_text(
        "Archive cleared: 2 image(s), 1 raw slot(s)"
    )
    expect(page.get_by_test_id("renders-summary")).to_contain_text("0 archived image(s)")
    assert archive.count() == 0
    assert not raw.exists()
    assert live_server.frames("wien") == frames
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_clear_archive_shows_server_error(ui):
    ui.route("**/api/archive/clear", lambda route: route.fulfill(
        status=503, content_type="application/json", body='{"detail":"Render still running"}',
    ))
    ui.once("dialog", lambda dialog: dialog.accept())
    ui.get_by_test_id("archive-clear").click()
    expect(ui.get_by_test_id("archive-clear-message")).to_contain_text("Failed: Render still running")
    expect(ui.get_by_test_id("archive-clear")).to_be_enabled()


def test_archive_all_now_renders_every_image_type(ui, live_server):
    from ha_satimage.config import default_config
    from ha_satimage.sources import archive_composites

    expected_types = sorted(archive_composites(default_config().sources.get("dummy")))
    ui.get_by_test_id("archive-render-now").click()
    expect(ui.get_by_test_id("archive-render-message")).to_contain_text("Archiving")

    renders = live_server.data_dir / "frames" / "_renders" / "wien" / "dummy"
    for _ in range(60):
        if all(
            (renders / composite).is_dir() and any((renders / composite).glob("*.jpg"))
            for composite in expected_types
        ):
            break
        ui.wait_for_timeout(500)
    types = sorted(p.name for p in renders.iterdir() if p.is_dir())
    assert types == expected_types

    ui.reload()
    ui.get_by_test_id("archive-wien").click()
    expect(ui.get_by_test_id("viewer-source")).to_have_value("dummy")
    options = ui.get_by_test_id("viewer-composite").locator("option")
    expect(options).to_have_count(len(types))
    _image_loaded(ui, "viewer-image")


# --- Version / language -----------------------------------------------------

def test_app_version_is_shown(ui, live_server):
    from ha_satimage import __version__

    expect(ui.get_by_test_id("app-version")).to_have_text(f"v{__version__}")
    expect(ui.get_by_test_id("api-link-version")).to_have_attribute("href", "/api/version")
    response = ui.request.get(live_server.url + "/api/version")
    assert response.json() == {"version": __version__}


def test_page_is_english(ui):
    expect(ui.get_by_test_id("sources-table").locator("tbody tr").first).to_be_visible()
    assert ui.locator("html").get_attribute("lang") == "en"
    text = ui.locator("body").inner_text()
    for word in (" und ", "Speichern", "Einstellungen", "Bild ", "Aktualisieren", "Quelle", "ä", "ö", "ü", "ß"):
        assert word not in text, f"German text found: {word!r}"


# --- Locations on the map of Europe (location_change) -------------------------

# Map extent (lon_min, lat_min, lon_max, lat_max), see location_change.EUROPE_EXTENT.
_EXTENT = (-25.0, 33.0, 45.0, 72.0)


def _map_point(page, lat: float, lon: float) -> tuple[float, float]:
    """Screen coordinates of lat/lon on the (possibly zoomed) location map."""
    box = page.evaluate(
        "() => { const r = document.querySelector('[data-testid=\"location-canvas\"]').getBoundingClientRect();"
        " return { x: r.left, y: r.top, w: r.width, h: r.height }; }"
    )
    lon_min, lat_min, lon_max, lat_max = _EXTENT
    return (
        box["x"] + (lon - lon_min) / (lon_max - lon_min) * box["w"],
        box["y"] + (lat_max - lat) / (lat_max - lat_min) * box["h"],
    )


def _picked(page) -> tuple[float, float]:
    return (
        float(page.get_by_test_id("location-lat").input_value()),
        float(page.get_by_test_id("location-lon").input_value()),
    )


def test_add_location_by_clicking_on_the_map(ui, live_server):
    expect(ui.get_by_test_id("location-count")).to_contain_text("2 of max. 4")
    ui.get_by_test_id("location-add").click()
    editor = ui.get_by_test_id("location-editor")
    expect(editor).to_be_visible()
    expect(ui.get_by_test_id("location-title")).to_have_text("Add location")
    _image_loaded(ui, "location-map-image")
    assert "outline.png" in ui.get_by_test_id("location-map-image").get_attribute("src")
    # Existing locations are shown on the map.
    expect(ui.get_by_test_id("location-box-wien")).to_be_visible()
    expect(ui.get_by_test_id("location-box-mallorca")).to_be_visible()
    expect(ui.get_by_test_id("location-selection")).to_be_hidden()

    # Saving without a centre explains what is missing.
    ui.get_by_test_id("location-save").click()
    expect(ui.get_by_test_id("location-message")).to_contain_text("Mark the centre")

    # Zoom in towards Berlin, then click exactly on it.
    x, y = _map_point(ui, 52.52, 13.405)
    ui.mouse.move(x, y)
    ui.mouse.wheel(0, -300)
    ui.mouse.wheel(0, -300)
    assert float(editor.get_attribute("data-zoom")) > 1.4
    x, y = _map_point(ui, 52.52, 13.405)
    ui.mouse.click(x, y)
    lat, lon = _picked(ui)
    assert abs(lat - 52.52) < 0.1 and abs(lon - 13.405) < 0.1
    expect(ui.get_by_test_id("location-selection")).to_be_visible()
    expect(ui.get_by_test_id("location-marker")).to_be_visible()

    ui.get_by_test_id("location-name").fill("berlin")
    ui.get_by_test_id("location-radius").fill("200")
    expect(ui.get_by_test_id("location-radius-range")).to_have_value("200")
    expect(ui.get_by_test_id("location-selection")).to_contain_text("berlin · 200 km")
    ui.get_by_test_id("location-save").click()

    # Page reloads with the new location card; it renders immediately.
    expect(ui.get_by_test_id("region-berlin")).to_be_visible()
    expect(editor).to_be_hidden()
    expect(ui.get_by_test_id("location-count")).to_contain_text("3 of max. 4")
    berlin = next(r for r in _stored(live_server)["regions"] if r["name"] == "berlin")
    assert abs(berlin["lat"] - 52.52) < 0.1 and abs(berlin["lon"] - 13.405) < 0.1
    assert berlin["radius_km"] == 200
    expect(ui.get_by_test_id("region-position-berlin")).to_contain_text("200.0 km")
    live_server.ensure_frames("berlin", 1)


def test_location_entered_as_lat_lon_and_errors_are_shown(ui, live_server):
    ui.get_by_test_id("location-add").click()
    ui.get_by_test_id("location-name").fill("wien")  # already exists
    ui.get_by_test_id("location-lat").fill("47.07")
    ui.get_by_test_id("location-lon").fill("15.44")
    expect(ui.get_by_test_id("location-selection")).to_be_visible()
    ui.get_by_test_id("location-save").click()
    expect(ui.get_by_test_id("location-message")).to_contain_text("already exists")
    ui.get_by_test_id("location-name").fill("graz")
    ui.get_by_test_id("location-save").click()
    expect(ui.get_by_test_id("region-graz")).to_be_visible()
    graz = next(r for r in _stored(live_server)["regions"] if r["name"] == "graz")
    assert (graz["lat"], graz["lon"], graz["radius_km"]) == (47.07, 15.44, 300.0)


def test_at_most_four_locations_in_the_ui(ui, live_server):
    for name, lat, lon in (("berlin", 52.5, 13.4), ("rome", 41.9, 12.5)):
        response = ui.request.post(
            live_server.url + "/api/regions", data={"name": name, "lat": lat, "lon": lon}
        )
        assert response.ok
    ui.reload()
    expect(ui.get_by_test_id("location-count")).to_contain_text("4 of max. 4")
    expect(ui.get_by_test_id("location-add")).to_be_disabled()
    expect(ui.get_by_test_id("location-limit")).to_be_visible()
    # Deep link to "new" does not open the editor at the limit.
    ui.goto(live_server.url + "/#location=new")
    ui.wait_for_timeout(500)
    expect(ui.get_by_test_id("location-editor")).to_be_hidden()

    ui.once("dialog", lambda dialog: dialog.accept())
    ui.get_by_test_id("location-remove-rome").click()
    expect(ui.get_by_test_id("region-rome")).to_have_count(0)
    expect(ui.get_by_test_id("location-add")).to_be_enabled()
    expect(ui.get_by_test_id("location-limit")).to_be_hidden()
    assert "rome" not in [r["name"] for r in _stored(live_server)["regions"]]


def test_remove_can_be_cancelled(ui, live_server):
    ui.once("dialog", lambda dialog: dialog.dismiss())
    ui.get_by_test_id("location-remove-mallorca").click()
    ui.wait_for_timeout(300)
    expect(ui.get_by_test_id("region-mallorca")).to_be_visible()
    assert "mallorca" in [r["name"] for r in _stored(live_server)["regions"]]


def test_change_location_moves_region_and_resets_history(ui, live_server):
    before = {f["filename"] for f in live_server.ensure_frames("wien", 2)}
    ui.get_by_test_id("location-edit-wien").click()
    expect(ui.get_by_test_id("location-title")).to_have_text("Change location: wien")
    expect(ui.get_by_test_id("location-name")).to_be_hidden()
    assert _picked(ui) == (48.2082, 16.3738)
    expect(ui.get_by_test_id("location-selection")).to_be_visible()
    expect(ui.get_by_test_id("location-box-wien")).to_have_count(0)  # it is the selection
    expect(ui.get_by_test_id("location-box-mallorca")).to_be_visible()

    # Move to Salzburg by clicking on the map, smaller radius via the slider.
    _image_loaded(ui, "location-map-image")
    x, y = _map_point(ui, 47.8, 13.04)
    ui.mouse.click(x, y)
    ui.get_by_test_id("location-radius-range").fill("150")
    expect(ui.get_by_test_id("location-radius")).to_have_value("150")
    ui.get_by_test_id("location-save").click()

    expect(ui.get_by_test_id("location-editor")).to_be_hidden()
    expect(ui.get_by_test_id("region-position-wien")).to_contain_text("150.0 km")
    wien = next(r for r in _stored(live_server)["regions"] if r["name"] == "wien")
    assert abs(wien["lat"] - 47.8) < 0.15 and abs(wien["lon"] - 13.04) < 0.15
    assert wien["radius_km"] == 150
    # The old history is dropped, the new location renders right away.
    frames = live_server.ensure_frames("wien", 1)
    assert not {f["filename"] for f in frames} & before


def test_location_map_zoom_pan_and_cancel(ui, live_server):
    ui.get_by_test_id("location-edit-mallorca").click()
    editor = ui.get_by_test_id("location-editor")
    _image_loaded(ui, "location-map-image")
    assert editor.get_attribute("data-zoom") == "1.00"
    ui.get_by_test_id("location-zoom-in").click()
    ui.get_by_test_id("location-zoom-in").click()
    assert float(editor.get_attribute("data-zoom")) > 2
    # Dragging pans the map and does not move the location.
    before = ui.get_by_test_id("location-canvas").bounding_box()
    stage = ui.get_by_test_id("location-map").bounding_box()
    cx, cy = stage["x"] + stage["width"] / 2, stage["y"] + stage["height"] / 2
    ui.mouse.move(cx, cy)
    ui.mouse.down()
    ui.mouse.move(cx + 80, cy + 40, steps=5)
    ui.mouse.up()
    after = ui.get_by_test_id("location-canvas").bounding_box()
    assert abs(after["x"] - before["x"] - 80) < 2 and abs(after["y"] - before["y"] - 40) < 2
    assert _picked(ui) == (39.6953, 3.0176)
    # A click after zooming and panning still hits the right place.
    canvas = ui.get_by_test_id("location-canvas").bounding_box()
    x, y = cx + 30, cy - 20
    lon_min, lat_min, lon_max, lat_max = _EXTENT
    expected_lon = lon_min + (x - canvas["x"]) / canvas["width"] * (lon_max - lon_min)
    expected_lat = lat_max - (y - canvas["y"]) / canvas["height"] * (lat_max - lat_min)
    ui.mouse.click(x, y)
    lat, lon = _picked(ui)
    assert abs(lat - expected_lat) < 0.05 and abs(lon - expected_lon) < 0.05
    ui.get_by_test_id("location-zoom-out").click()
    ui.get_by_test_id("location-zoom-reset").click()
    assert editor.get_attribute("data-zoom") == "1.00"

    # Esc/cancel discards the change.
    ui.keyboard.press("Escape")
    expect(editor).to_be_hidden()
    mallorca = next(r for r in _stored(live_server)["regions"] if r["name"] == "mallorca")
    assert (mallorca["lat"], mallorca["lon"]) == (39.6953, 3.0176)


def test_satellite_map_is_rendered_on_demand_and_errors_are_shown(ui):
    ui.get_by_test_id("location-add").click()
    background = ui.get_by_test_id("location-background")
    expect(background).to_have_value("outline")
    expect(ui.get_by_test_id("location-satellite-info")).to_contain_text("not loaded yet")
    # Test server has no EUMETSAT credentials: the download fails cleanly.
    ui.get_by_test_id("location-satellite-render").click()
    expect(ui.get_by_test_id("location-message")).to_contain_text("Satellite map failed")
    expect(ui.get_by_test_id("location-satellite-info")).to_contain_text("not available")
    expect(background).to_have_value("outline")
    expect(ui.get_by_test_id("location-satellite-render")).to_be_enabled()


def test_location_editor_on_the_phone(mobile_page, live_server):
    page = mobile_page
    page.goto(live_server.url + "/")
    page.get_by_test_id("location-edit-wien").tap()
    editor = page.get_by_test_id("location-editor")
    expect(editor).to_be_visible()
    _image_loaded(page, "location-map-image")
    box = editor.bounding_box()
    assert abs(box["width"] - 390) < 2 and abs(box["height"] - 844) < 2
    for testid in ("location-map", "location-zoom-in", "location-close", "location-lat", "location-radius"):
        expect(page.get_by_test_id(testid)).to_be_in_viewport()
    assert page.evaluate("document.documentElement.scrollWidth") <= 390

    page.get_by_test_id("location-zoom-in").tap()
    assert float(editor.get_attribute("data-zoom")) > 1
    x, y = _map_point(page, 47.07, 15.44)  # Graz
    page.touchscreen.tap(x, y)
    lat, lon = _picked(page)
    assert abs(lat - 47.07) < 0.15 and abs(lon - 15.44) < 0.15

    # The phone's back button closes the editor without saving.
    page.go_back()
    expect(editor).to_be_hidden()
    wien = next(r for r in _stored(live_server)["regions"] if r["name"] == "wien")
    assert wien["lat"] == 48.2082

    # Save on the phone.
    page.get_by_test_id("location-edit-wien").tap()
    expect(page.get_by_test_id("location-lat")).to_have_value("48.2082")  # editor reset
    _image_loaded(page, "location-map-image")
    x, y = _map_point(page, 47.07, 15.44)
    page.touchscreen.tap(x, y)
    assert abs(_picked(page)[0] - 47.07) < 0.2
    page.get_by_test_id("location-save").scroll_into_view_if_needed()
    page.get_by_test_id("location-save").tap()
    expect(editor).to_be_hidden()
    wien = next(r for r in _stored(live_server)["regions"] if r["name"] == "wien")
    assert abs(wien["lat"] - 47.07) < 0.2


def test_location_deep_link_opens_editor(page, live_server):
    page.goto(live_server.url + "/#location=mallorca")
    expect(page.get_by_test_id("location-editor")).to_be_visible()
    expect(page.get_by_test_id("location-title")).to_have_text("Change location: mallorca")
    page.get_by_test_id("location-close").click()
    expect(page.get_by_test_id("location-editor")).to_be_hidden()
    assert "location=" not in page.url


SECTIONS = ["sources", "storage", "credentials", "status", "logs", "api"]


@pytest.mark.keep_folded
def test_sections_collapsed_by_default_and_foldable(page, live_server):
    page.goto(live_server.url + "/")
    expect(page.get_by_test_id("sec-regions").get_by_test_id("location-add")).to_be_visible()
    expect(page.get_by_test_id("sec-regions-fold")).to_have_count(0)
    for name in SECTIONS:
        fold = page.get_by_test_id(f"sec-{name}-fold")
        expect(fold).not_to_have_attribute("open", "")
    expect(page.get_by_test_id("credentials-message")).to_be_hidden()
    page.get_by_test_id("sec-credentials-fold").locator("summary").click()
    expect(page.get_by_test_id("consumer-key")).to_be_visible()
    page.get_by_test_id("sec-credentials-fold").locator("summary").click()
    expect(page.get_by_test_id("consumer-key")).to_be_hidden()


@pytest.mark.keep_folded
def test_sections_deep_link_opens_section(page, live_server):
    page.goto(live_server.url + "/#sec-api")
    expect(page.get_by_test_id("sec-api-fold")).to_have_attribute("open", "")
    expect(page.get_by_test_id("sec-logs-fold")).not_to_have_attribute("open", "")
