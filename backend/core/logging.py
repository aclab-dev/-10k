"""Centralized structlog setup — JSON output, configurable level, secret scrubbing."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_MASKED = "***"

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "secret",
        "secret_key",
        "password",
        "passwd",
        "passphrase",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "auth",
        "private_key",
        "credential",
        "credentials",
    }
)


def _scrub_secrets(
    logger: Any, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _SECRET_KEYS:
            event_dict[key] = _MASKED
    return event_dict


def configure_logging(level: str | None = None) -> None:
    """Configure structlog once at startup.

    Reads LOG_LEVEL env var if *level* is not given. Defaults to INFO.
    Call this as early as possible in the process entry point.
    """
    log_level_name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )
    logging.root.setLevel(log_level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.ExceptionRenderer(),
            _scrub_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
