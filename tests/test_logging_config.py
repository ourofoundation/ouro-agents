import logging

from ouro_agents.logging_config import OuroAgentsFormatter


def test_formatter_shortens_package_logger_names():
    formatter = OuroAgentsFormatter(tag="ouro-agents", use_colors=False)
    record = logging.LogRecord(
        name="ouro_agents.scheduler",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Scheduler started",
        args=(),
        exc_info=None,
    )
    line = formatter.format(record)
    assert line.endswith("scheduler: Scheduler started")
    assert "[ouro-agents]" not in line


def test_formatter_keeps_third_party_logger_names():
    formatter = OuroAgentsFormatter(tag="ouro-agents", use_colors=False)
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Application startup complete.",
        args=(),
        exc_info=None,
    )
    line = formatter.format(record)
    assert line.endswith("uvicorn.error: Application startup complete.")
    assert "[ouro-agents]" not in line
