"""Uvicorn / app logging — compact stderr lines with optional color."""

from __future__ import annotations

import logging
from typing import Any

from ouro_mcp.logging_config import _DIM, _RESET, TaggedColoredFormatter

_DATE_FMT = "%Y-%m-%d %H:%M:%S"
_PKG_PREFIX = "ouro_agents."


class OuroAgentsFormatter(TaggedColoredFormatter):
    """Drop the redundant ``[ouro-agents]`` tag; shorten ``ouro_agents.*`` logger names."""

    def format(self, record: logging.LogRecord) -> str:
        colors = self._colors()
        ts = self.formatTime(record, self.datefmt)
        if colors:
            ts = f"{_DIM}{ts}{_RESET}"
        levelname = self._paint_level(record.levelno, record.levelname, colors)
        name = record.name
        if name.startswith(_PKG_PREFIX):
            name = name[len(_PKG_PREFIX) :]
        body = f"{ts} {levelname} {name}: {record.getMessage()}"
        if record.exc_info:
            body += "\n" + self.formatException(record.exc_info)
        return body


class OuroAgentsLogHandler(logging.Handler):
    """Emit logs through Rich so status spinners and log lines do not collide."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        try:
            from ouro_agents.display import get_display

            get_display().console.print(msg, markup=False, highlight=False)
        except Exception:
            self.handleError(record)


def uvicorn_log_config() -> dict[str, Any]:
    """Dict for :func:`uvicorn.run` ``log_config=``."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "ouro_agents": {
                "()": "ouro_agents.logging_config.OuroAgentsFormatter",
                "tag": "ouro-agents",
                "datefmt": _DATE_FMT,
            },
        },
        "handlers": {
            "default": {
                "formatter": "ouro_agents",
                "()": "ouro_agents.logging_config.OuroAgentsLogHandler",
            },
            "access": {
                "formatter": "ouro_agents",
                "()": "ouro_agents.logging_config.OuroAgentsLogHandler",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            # Startup chatter ("Uvicorn running on…", "Application startup complete")
            # is superseded by the startup summary; real problems log at WARNING+.
            "uvicorn.error": {"handlers": ["default"], "level": "WARNING", "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
            # APScheduler is chatty at INFO (job add/start); we log our own scheduler lines.
            "apscheduler": {"handlers": ["default"], "level": "WARNING", "propagate": False},
            # watchfiles logs every fs event at INFO even when uvicorn filters reload;
            # workspace/chroma writes are expected during agent runs.
            "watchfiles": {"handlers": ["default"], "level": "WARNING", "propagate": False},
            "watchfiles.main": {
                "handlers": ["default"],
                "level": "WARNING",
                "propagate": False,
            },
        },
        "root": {"handlers": ["default"], "level": "INFO"},
    }
