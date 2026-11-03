"""Scheduled maintenance of the EUMETSAT Rapid Scan Service (RSS).

EUMETSAT publishes planned RSS interruptions (monthly Full Earth Scanning,
ground segment maintenance, ...) on
https://user.eumetsat.int/resources/service-statuses/rss-schedule. During
such a window the RSS collection delivers no new products, so querying the
Data Store only produces errors/warnings in the log.

This module fetches that page (public, no authentication), extracts the
maintenance windows (start/end, UTC) and answers whether a collection is
currently under maintenance. The scheduler then skips the Data Store query
and shows a black "Currently unavailable due to maintenance" image instead.

The page is fetched lazily (only when an affected source is downloaded) and
at most every ``sources.maintenance_check_hours`` hours; the result is kept
in ``<data dir>/maintenance.json`` so it survives restarts.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ha_satimage.atomic import write_text_atomic

logger = logging.getLogger(__name__)

URL_ENV = "HA_SATIMAGE_MAINTENANCE_URL"
DEFAULT_URL = "https://user.eumetsat.int/resources/service-statuses/rss-schedule"
REQUEST_TIMEOUT = 20
# After a failed fetch, try again at the latest after this.
ERROR_RETRY = timedelta(minutes=30)
# Windows ended longer ago than this are dropped.
KEEP_PAST = timedelta(days=1)
# Two dates further apart than this are not a maintenance window.
MAX_WINDOW = timedelta(days=14)
# Collections served by the Rapid Scan Service (e.g. EO:EUM:DAT:MSG:MSG15-RSS).
AFFECTED_COLLECTIONS = re.compile(r"RSS", re.IGNORECASE)
# Frames rendered during maintenance carry this composite name.
MAINTENANCE_COMPOSITE = "maintenance"
TITLE = "Currently unavailable due to maintenance"


def page_url() -> str:
    return os.environ.get(URL_ENV, DEFAULT_URL)


def affects(collection: str | None) -> bool:
    return bool(collection) and bool(AFFECTED_COLLECTIONS.search(collection or ""))


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def active(self, now: datetime) -> bool:
        return self.start <= now < self.end

    def as_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}

    @classmethod
    def from_dict(cls, data: dict) -> "Window":
        return cls(datetime.fromisoformat(data["start"]), datetime.fromisoformat(data["end"]))

    def describe(self) -> str:
        fmt = "%Y-%m-%d %H:%M"
        return f"{self.start.strftime(fmt)} to {self.end.strftime(fmt)} UTC"


# -- Parsing -------------------------------------------------------------------
_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = r"(?P<mon>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_TIME = r"(?:(?:,|\s+at)?\s*(?P<hh>[01]?\d|2[0-3])[:h.](?P<mi>[0-5]\d)(?::[0-5]\d(?:\.\d+)?)?\s*(?:z|utc|gmt)?)?"
_DATE_PATTERNS = (
    # 2026-09-08T09:00:00Z, 2026-09-08 09:00, 2026/09/08 09:00
    re.compile(r"(?P<y>20\d\d)[-/](?P<m>[01]?\d)[-/](?P<d>[0-3]?\d)(?:t|\s*)" + _TIME, re.IGNORECASE),
    # 08/09/2026 09:00, 08.09.2026 09:00 (day first)
    re.compile(r"(?<!\d)(?P<d>[0-3]?\d)[./](?P<m>[01]?\d)[./](?P<y>20\d\d)" + _TIME, re.IGNORECASE),
    # 8 September 2026 09:00, 8 Sep 2026, 09:00 UTC
    re.compile(r"(?<!\d)(?P<d>[0-3]?\d)(?:st|nd|rd|th)?\s+" + _MONTH + r",?\s+(?P<y>20\d\d)" + _TIME, re.IGNORECASE),
    # September 8, 2026 09:00
    re.compile(_MONTH + r"\s+(?P<d>[0-3]?\d)(?:st|nd|rd|th)?,?\s+(?P<y>20\d\d)" + _TIME, re.IGNORECASE),
)
# "8 September 2026 09:00 - 10:30": end time on the same day.
_SAME_DAY_END = re.compile(
    r"\s*(?:-|–|—|to|until|till)\s*(?P<hh>[01]?\d|2[0-3])[:h.](?P<mi>[0-5]\d)(?!\d|[./-]\d)",
    re.IGNORECASE,
)
# Dates of the page itself, not of a maintenance window.
_IGNORE_BEFORE = re.compile(r"(updated|published|issued|revis|modified|posted|created)\W*\w*\W*$", re.IGNORECASE)
_BLOCK_TAGS = re.compile(r"<\s*/?\s*(tr|td|th|li|p|div|br|h\d|table|section|article|dd|dt)\b[^>]*>", re.IGNORECASE)


def _html_to_text(page: str) -> str:
    page = re.sub(r"<(style|noscript)\b.*?</\1\s*>", " ", page, flags=re.IGNORECASE | re.DOTALL)
    # Pages rendered client-side carry their data as (escaped) JSON in scripts.
    page = page.replace("\\u002F", "/").replace("\\/", "/").replace("\\n", "\n")
    page = _BLOCK_TAGS.sub("\n", page)
    page = re.sub(r"<[^>]+>", " ", page)
    return html.unescape(page)


@dataclass(frozen=True)
class _Stamp:
    at: datetime
    has_time: bool
    pos: int
    end_pos: int


def _stamps(text: str) -> list[_Stamp]:
    found: list[_Stamp] = []
    taken: list[tuple[int, int]] = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            if any(match.start() < e and s < match.end() for s, e in taken):
                continue
            parts = match.groupdict()
            month = _MONTHS[parts["mon"].lower()] if parts.get("mon") else int(parts["m"])
            try:
                day = datetime(int(parts["y"]), month, int(parts["d"]), tzinfo=timezone.utc)
            except ValueError:
                continue
            has_time = parts.get("hh") is not None
            at = day + timedelta(hours=int(parts["hh"]), minutes=int(parts["mi"])) if has_time else day
            taken.append((match.start(), match.end()))
            found.append(_Stamp(at, has_time, match.start(), match.end()))
    return sorted(found, key=lambda s: s.pos)


def parse_schedule(page: str) -> list[Window]:
    """Extract maintenance windows (UTC) from the schedule page.

    The page layout is not formally specified, so the parser is tolerant:
    dates in common notations are collected in document order and
    consecutive pairs (start < end, at most ``MAX_WINDOW`` apart) form a
    window. A date-only end covers the whole day; "09:00 - 10:30" after a
    date ends on the same day. Dates labelled as publication/update time
    are ignored.
    """
    text = _html_to_text(page)
    windows: set[Window] = set()
    pending: _Stamp | None = None
    for stamp in _stamps(text):
        if _IGNORE_BEFORE.search(text[max(0, stamp.pos - 40):stamp.pos]):
            continue
        same_day = _SAME_DAY_END.match(text, stamp.end_pos) if stamp.has_time else None
        if same_day:
            day = stamp.at.replace(hour=0, minute=0)
            end = day + timedelta(hours=int(same_day["hh"]), minutes=int(same_day["mi"]))
            if end <= stamp.at:
                end += timedelta(days=1)
            windows.add(Window(stamp.at, end))
            pending = None
            continue
        if pending is not None:
            end = stamp.at if stamp.has_time else stamp.at + timedelta(days=1)
            if pending.at < end and end - pending.at <= MAX_WINDOW:
                windows.add(Window(pending.at, end))
                pending = None
                continue
        pending = stamp
    return sorted(windows, key=lambda w: (w.start, w.end))


def _fetch(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"Accept": "text/html,application/json", "User-Agent": "ha_satimage"}
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


# -- Schedule store ------------------------------------------------------------
class MaintenanceSchedule:
    def __init__(self, state_path: Path) -> None:
        self._state_path = Path(state_path)
        self._lock = threading.Lock()
        # Serializes fetches without blocking readers of the state.
        self._fetch_lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            for item in state.get("windows", []):
                Window.from_dict(item)  # validate
            return state
        except (OSError, ValueError, KeyError, TypeError):
            return {"checked_at": None, "error": None, "url": None, "windows": []}

    def _save(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(self._state_path, json.dumps(self.state(), indent=2))
        except OSError as exc:
            logger.warning("Could not save maintenance schedule: %s", exc)

    def state(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def windows(self) -> list[Window]:
        return [Window.from_dict(w) for w in self.state().get("windows", [])]

    def _due(self, check_hours: int, now: datetime) -> bool:
        state = self.state()
        checked = state.get("checked_at")
        if not checked or state.get("url") != page_url():
            return True
        interval = timedelta(hours=check_hours)
        if state.get("error"):
            interval = min(interval, ERROR_RETRY)
        return datetime.fromisoformat(checked) + interval <= now

    def _set(self, state: dict) -> None:
        with self._lock:
            self._state = state
        self._save()

    def refresh(self, check_hours: int, force: bool = False) -> None:
        """Fetch the page again if the last check is older than ``check_hours``."""
        if check_hours <= 0:
            return
        with self._fetch_lock:
            now = datetime.now(timezone.utc)
            if not (force or self._due(check_hours, now)):
                return
            url = page_url()
            try:
                windows = parse_schedule(_fetch(url))
            except Exception as exc:
                logger.warning("Maintenance schedule %s could not be loaded: %s", url, exc)
                self._set({**self.state(), "checked_at": now.isoformat(), "error": str(exc), "url": url})
                return
            windows = [w for w in windows if w.end > now - KEEP_PAST]
            self._set({
                "checked_at": now.isoformat(),
                "error": None,
                "url": url,
                "windows": [w.as_dict() for w in windows],
            })
        upcoming = ", ".join(w.describe() for w in windows) or "none"
        logger.info("Maintenance schedule loaded from %s: %s", url, upcoming)

    def window_for(
        self, collection: str | None, check_hours: int, now: datetime | None = None
    ) -> Window | None:
        """Active maintenance window of the collection (refreshes if due)."""
        if check_hours <= 0 or not affects(collection):
            return None
        self.refresh(check_hours)
        now = now or datetime.now(timezone.utc)
        return next((w for w in self.windows() if w.active(now)), None)

    def next_window_for(
        self, collection: str | None, check_hours: int, now: datetime | None = None
    ) -> Window | None:
        """Active or next upcoming window (cached state only, no fetch)."""
        if check_hours <= 0 or not affects(collection):
            return None
        now = now or datetime.now(timezone.utc)
        return next((w for w in self.windows() if w.end > now), None)


# -- Image -----------------------------------------------------------------------
def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):  # pragma: no cover - Pillow without FreeType
        return ImageFont.load_default()


def render_maintenance_png(width: int, height: int, window: Window) -> bytes:
    """Black image: title, second line the maintenance time frame."""
    image = Image.new("RGB", (width, height), color=(0, 0, 0))
    draw = ImageDraw.Draw(image)
    lines = (TITLE, f"From {window.describe()}")
    size = max(10, width // 26)
    for _ in range(20):
        fonts = (_font(size), _font(max(8, int(size * 0.8))))
        boxes = [draw.textbbox((0, 0), text, font=font) for text, font in zip(lines, fonts)]
        if max(b[2] - b[0] for b in boxes) <= width * 0.92 or size <= 8:
            break
        size = int(size * 0.9)
    heights = [b[3] - b[1] for b in boxes]
    gap = heights[0] // 2
    y = (height - sum(heights) - gap) // 2
    for text, font, box, line_height in zip(lines, fonts, boxes, heights):
        x = (width - (box[2] - box[0])) // 2 - box[0]
        draw.text((x, y - box[1]), text, font=font, fill=(255, 255, 255))
        y += line_height + gap
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
