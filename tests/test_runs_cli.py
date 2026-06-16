"""Smoke tests for the ``ouro-agents runs`` CLI commands."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from ouro_agents.cli import cli
from ouro_agents.run_log import RunLogStore, RunRecord, RunStepRecord

runner = CliRunner()


def _write_config(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = {
        "agent": {"name": "testbot", "model": "test/model", "workspace": str(workspace)},
        "heartbeat": {"model": "test/model"},
        "mcp_servers": [],
        "memory": {"extraction_model": "test/x", "embedder": "test/e"},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    return config_path, workspace


def _seed(workspace):
    store = RunLogStore(workspace / "runs.db")
    rec = RunRecord(
        run_id="run-abc123",
        mode="chat",
        team_id="t1",
        task="What teams am I on?",
        result="team-alpha",
        status="success",
        total_tokens=42,
        cost_usd=0.01,
    )
    rec.set_steps(
        [
            RunStepRecord(
                step_number=1,
                step_type="action",
                tool_calls=[{"name": "get_teams", "args": {}}],
                observations="team-alpha",
            )
        ]
    )
    store.write(rec)
    store.write(RunRecord(run_id="run-err", mode="heartbeat", status="error", task="tick"))
    store.close()


def test_runs_list(tmp_path):
    config_path, workspace = _write_config(tmp_path)
    _seed(workspace)
    result = runner.invoke(cli, ["--config", str(config_path), "runs", "list"])
    assert result.exit_code == 0, result.output
    # Table headers are stable regardless of terminal width; row content is
    # asserted via --json in test_runs_list_json_and_filter.
    assert "mode" in result.output
    assert "status" in result.output


def test_runs_list_json_and_filter(tmp_path):
    config_path, workspace = _write_config(tmp_path)
    _seed(workspace)
    result = runner.invoke(
        cli, ["--config", str(config_path), "runs", "list", "--status", "error", "--json"]
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert {r["run_id"] for r in rows} == {"run-err"}


def test_runs_show_by_prefix(tmp_path):
    config_path, workspace = _write_config(tmp_path)
    _seed(workspace)
    result = runner.invoke(
        cli, ["--config", str(config_path), "runs", "show", "run-abc"]
    )
    assert result.exit_code == 0, result.output
    assert "What teams am I on?" in result.output
    assert "get_teams" in result.output


def test_runs_stats(tmp_path):
    config_path, workspace = _write_config(tmp_path)
    _seed(workspace)
    result = runner.invoke(cli, ["--config", str(config_path), "runs", "stats"])
    assert result.exit_code == 0, result.output
    assert "total" in result.output


def test_runs_list_empty_when_no_db(tmp_path):
    config_path, _ = _write_config(tmp_path)
    result = runner.invoke(cli, ["--config", str(config_path), "runs", "list"])
    assert result.exit_code == 0, result.output
    assert "No matching runs" in result.output
