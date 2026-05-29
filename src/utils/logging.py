"""structlog setup. Phase 1.

JSON logging to stdout per CLAUDE.md section 9. Call ``setup_logging()`` once at
process start (before anything logs), then ``get_logger(__name__)`` everywhere.
Docker handles rotation via the json-file driver.
"""

from __future__ import annotations

import logging
import sys
from typing import cast

import structlog

_CONFIGURED = False


def setup_logging() -> None:
    """Configure stdlib logging + structlog to emit JSON lines to stdout.

    structlog and the stdlib root logger share a single ``ProcessorFormatter``
    so that logs from third-party libraries (aiogram, httpx, uvicorn) are
    rendered as JSON too. Idempotent.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    from src.config import settings

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # Processors shared by both structlog-native and stdlib-routed records.
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # Hand off to the stdlib ProcessorFormatter for final rendering.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain runs on records coming from stdlib loggers.
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger. Pass ``__name__`` from the call site."""
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))
