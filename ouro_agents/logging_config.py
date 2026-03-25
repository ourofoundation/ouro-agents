"""Uvicorn / app logging aligned with ouro-mcp plain stderr lines (with optional color)."""

from __future__ import annotations

from typing import Any

_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def uvicorn_log_config() -> dict[str, Any]:
    """Dict for :func:`uvicorn.run` ``log_config=`` — same line shape as ouro-mcp ``plain`` style."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "ouro_agents": {
                "()": "ouro_mcp.logging_config.TaggedColoredFormatter",
                "tag": "ouro-agents",
                "datefmt": _DATE_FMT,
            },
        },
        "handlers": {
            "default": {
                "formatter": "ouro_agents",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": "ouro_agents",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
            # APScheduler is chatty at INFO (job add/start); we log our own scheduler lines.
            "apscheduler": {"handlers": ["default"], "level": "WARNING", "propagate": False},
        },
        "root": {"handlers": ["default"], "level": "INFO"},
    }
