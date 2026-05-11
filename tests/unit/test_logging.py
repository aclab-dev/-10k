"""Tests for backend.core.logging — secret scrubbing, level control, JSON output."""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest
import structlog

from backend.core.logging import _SECRET_KEYS, _scrub_secrets, configure_logging


# ---------------------------------------------------------------------------
# _scrub_secrets (pure unit)
# ---------------------------------------------------------------------------


class TestScrubSecrets:
    def test_masks_api_key(self) -> None:
        event: dict = {"event": "connect", "api_key": "sk-real"}
        assert _scrub_secrets(None, "info", event)["api_key"] == "***"

    def test_masks_api_secret(self) -> None:
        event: dict = {"event": "connect", "api_secret": "topsecret"}
        assert _scrub_secrets(None, "info", event)["api_secret"] == "***"

    def test_masks_password(self) -> None:
        event: dict = {"event": "db_connect", "password": "hunter2"}
        assert _scrub_secrets(None, "info", event)["password"] == "***"

    def test_masks_token(self) -> None:
        event: dict = {"event": "auth", "token": "abc123"}
        assert _scrub_secrets(None, "info", event)["token"] == "***"

    def test_masks_secret(self) -> None:
        event: dict = {"event": "init", "secret": "shh"}
        assert _scrub_secrets(None, "info", event)["secret"] == "***"

    def test_leaves_safe_keys_intact(self) -> None:
        event: dict = {"event": "order_placed", "symbol": "BTCUSDT", "qty": 0.01}
        result = _scrub_secrets(None, "info", event)
        assert result["symbol"] == "BTCUSDT"
        assert result["qty"] == 0.01
        assert result["event"] == "order_placed"

    def test_all_secret_keys_are_masked(self) -> None:
        event: dict = {k: "sensitive" for k in _SECRET_KEYS}
        event["event"] = "test"
        result = _scrub_secrets(None, "info", event)
        for key in _SECRET_KEYS:
            assert result[key] == "***", f"Expected {key} to be masked"


# ---------------------------------------------------------------------------
# configure_logging — level control
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    def test_defaults_to_info_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        configure_logging()
        assert logging.getLogger().level <= logging.INFO

    def test_respects_log_level_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        configure_logging(level=None)
        assert logging.getLogger().level == logging.WARNING

    def test_explicit_level_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "ERROR")
        configure_logging(level="DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_invalid_level_falls_back_to_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        configure_logging(level="INVALID_LEVEL")
        assert logging.getLogger().level == logging.INFO


# ---------------------------------------------------------------------------
# configure_logging — JSON output + scrubbing (end-to-end via isolated config)
# ---------------------------------------------------------------------------


class TestLoggingOutput:
    def _reconfigure(self, buf: StringIO, level: str = "DEBUG") -> None:
        """Reset structlog and wire it to write into *buf*."""
        structlog.reset_defaults()
        log_level = getattr(logging, level, logging.DEBUG)
        structlog.configure(
            processors=[
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                _scrub_secrets,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(log_level),
            logger_factory=structlog.PrintLoggerFactory(file=buf),
            cache_logger_on_first_use=False,
        )

    def test_output_is_valid_json(self) -> None:
        buf = StringIO()
        self._reconfigure(buf)
        structlog.get_logger().info("trade_executed", symbol="BTCUSDT")
        lines = [line for line in buf.getvalue().strip().splitlines() if line]
        assert lines, "No log output captured"
        parsed = json.loads(lines[-1])
        assert parsed["event"] == "trade_executed"
        assert "timestamp" in parsed
        assert parsed["level"] == "info"

    def test_secrets_scrubbed_in_json_output(self) -> None:
        buf = StringIO()
        self._reconfigure(buf)
        structlog.get_logger().info("exchange_connect", api_key="real-secret-key", exchange="bingx")
        output = buf.getvalue()
        assert "real-secret-key" not in output
        parsed = json.loads(output.strip().splitlines()[-1])
        assert parsed["api_key"] == "***"
        assert parsed["exchange"] == "bingx"

    def test_non_secret_fields_preserved_in_output(self) -> None:
        buf = StringIO()
        self._reconfigure(buf)
        structlog.get_logger().info("position_opened", symbol="ETHUSDT", leverage=5)
        parsed = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert parsed["symbol"] == "ETHUSDT"
        assert parsed["leverage"] == 5
