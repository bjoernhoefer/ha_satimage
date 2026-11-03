"""Log levels (environment defaults, runtime override) and protection logs."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from ha_satimage import logbuffer
from ha_satimage.backoff import FailureBackoff
from ha_satimage.config import AppConfig, LoggingConfig


@pytest.fixture
def handlers(monkeypatch):
    record = logging.Handler()
    stream = logging.StreamHandler()
    monkeypatch.setattr(logbuffer, "_record_handlers", [record])
    monkeypatch.setattr(logbuffer, "_stream_handler", stream)
    app_logger = logging.getLogger(logbuffer.APP_LOGGER)
    previous = app_logger.level
    yield record, stream
    app_logger.setLevel(previous)


def test_environment_defaults(monkeypatch, handlers):
    record, stream = handlers
    monkeypatch.setenv("HA_SATIMAGE_LOG_LEVEL", "warning")
    monkeypatch.setenv("HA_SATIMAGE_STDOUT_LOG_LEVEL", "ERROR")
    assert logbuffer.apply_levels() == ("WARNING", "ERROR")
    assert record.level == logging.WARNING and stream.level == logging.ERROR


def test_unknown_environment_value_falls_back_to_info(monkeypatch, handlers):
    monkeypatch.setenv("HA_SATIMAGE_LOG_LEVEL", "loud")
    monkeypatch.delenv("HA_SATIMAGE_STDOUT_LOG_LEVEL", raising=False)
    assert logbuffer.apply_levels() == ("INFO", "INFO")


def test_configuration_overrides_environment(monkeypatch, handlers):
    record, stream = handlers
    monkeypatch.setenv("HA_SATIMAGE_LOG_LEVEL", "INFO")
    monkeypatch.setenv("HA_SATIMAGE_STDOUT_LOG_LEVEL", "WARNING")
    assert logbuffer.apply_levels("ERROR", "DEBUG") == ("ERROR", "DEBUG")
    assert record.level == logging.ERROR and stream.level == logging.DEBUG
    # Empty = back to the environment default.
    assert logbuffer.apply_levels("", "") == ("INFO", "WARNING")


def test_debug_only_for_own_loggers(handlers):
    logbuffer.apply_levels("DEBUG", "WARNING")
    assert logging.getLogger("ha_satimage.scheduler").isEnabledFor(logging.DEBUG)
    assert logging.getLogger(logbuffer.APP_LOGGER).level == logging.DEBUG
    logbuffer.apply_levels("INFO", "WARNING")
    assert logging.getLogger(logbuffer.APP_LOGGER).level == logging.NOTSET


def test_logging_config_is_validated():
    assert LoggingConfig(level="debug").level == "DEBUG"
    assert AppConfig().logging == LoggingConfig(level="", stdout_level="")
    with pytest.raises(ValueError):
        LoggingConfig(stdout_level="LOUD")


def test_backoff_logs_repeated_failures_and_recovery(caplog):
    backoff = FailureBackoff()
    key = ("wien", "msg_seviri", "natural_color")
    now = datetime.now(timezone.utc)
    with caplog.at_level(logging.INFO, logger="ha_satimage.backoff"):
        backoff.failure(key, "OOM", now)
        backoff.failure(key, "OOM", now + timedelta(minutes=20))
        backoff.success(key)
        backoff.success(key)  # no pause: nothing to report
        backoff.failure(key, "OOM", now)
        backoff.clear()
    messages = [r.getMessage() for r in caplog.records]
    assert "wien/msg_seviri/natural_color failed 2 times in a row - pause now 30 min" in messages
    assert "wien/msg_seviri/natural_color works again after 2 failure(s) - pause lifted" in messages
    assert sum("works again" in m for m in messages) == 1
    assert "Configuration changed - 1 paused combination(s) are retried" in messages


def test_protection_summary_names_the_limits(monkeypatch):
    from ha_satimage.sources import satpy_render

    monkeypatch.setenv("HA_SATIMAGE_RENDER_MEMORY_MB", "500")
    summary = satpy_render.protection_summary()
    assert "memory budget 500 MiB" in summary
    assert "start method" in summary and "coarsened" in summary
