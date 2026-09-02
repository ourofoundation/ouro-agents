import json
from pathlib import Path

import pytest

from ouro_agents.config import MemoryConfig
from ouro_agents.memory.friction import FrictionEntry, FrictionQueue
from ouro_agents.tools.dream_tools import make_dream_tools
from ouro_agents.tools.workspace_layout import dream_write_scope


class _Backend:
    def get_all(self, **kwargs):
        return []


def _tool(tools, name):
    return next(item for item in tools if item.name == name)


def _make_tools(tmp_path: Path, *, dry_run: bool = False):
    queue = FrictionQueue.for_workspace(tmp_path)
    queue.enqueue(
        FrictionEntry(
            id="friction-1",
            kind="wasted_steps",
            evidence="Repeated the same lookup three times.",
            run_id="run-before",
        )
    )
    tools, state = make_dream_tools(
        workspace=tmp_path,
        agent_name="hermes",
        backend=_Backend(),
        memory_config=MemoryConfig(
            extraction_model="test",
            embedder="test",
        ),
        maintenance_model=object(),
        doc_store=None,
        run_id="dream-run",
        context={"window": {"new_runs": 4}},
        friction_queue=queue,
        dry_run=dry_run,
        max_changes=2,
    )
    return tools, state, queue


def test_dream_tools_write_report_proposal_and_resolve_friction(tmp_path: Path):
    (tmp_path / "skills").mkdir()
    tools, state, queue = _make_tools(tmp_path)

    with dream_write_scope(
        tmp_path,
        writable=["skills", "NOTES.md", "HEARTBEAT.md"],
        proposal_only=["SOUL.md", "skills:always"],
    ):
        written = json.loads(
            _tool(tools, "write_workspace_doc").forward(
                "NOTES.md",
                "Use fresh evidence.\n",
                "A stale note caused a repeated lookup.",
            )
        )
        proposal = json.loads(
            _tool(tools, "propose_change").forward(
                "SOUL.md",
                "Two recent corrections conflict with the current instruction.",
                "- old\n+ new",
            )
        )
        resolved = json.loads(
            _tool(tools, "resolve_friction").forward(
                ["friction-1"],
                "fixed",
                "Updated NOTES.md.",
            )
        )
        report = json.loads(
            _tool(tools, "write_dream_report").forward(
                "# Review\n\nOne note was corrected.",
                [],
                ["The repeated lookup should not recur."],
            )
        )

    assert written["status"] == "written"
    assert (tmp_path / "NOTES.md").read_text() == "Use fresh evidence.\n"
    assert Path(proposal["path"]).exists()
    assert resolved["count"] == 1
    assert queue.pending() == []
    assert Path(report["report_path"]).exists()
    assert state.expectations == ["The repeated lookup should not recur."]


def test_dream_tools_respect_proposal_only_and_dry_run(tmp_path: Path):
    (tmp_path / "skills").mkdir()
    (tmp_path / "SOUL.md").write_text("identity\n")
    tools, _, _ = _make_tools(tmp_path, dry_run=True)

    with dream_write_scope(
        tmp_path,
        writable=["skills", "NOTES.md", "HEARTBEAT.md"],
        proposal_only=["SOUL.md", "skills:always"],
    ):
        with pytest.raises(PermissionError, match="proposal-only"):
            _tool(tools, "write_workspace_doc").forward(
                "SOUL.md", "changed\n", "reason"
            )
        with pytest.raises(PermissionError, match="proposal-only"):
            _tool(tools, "append_workspace_doc").forward(
                "skills/new-always.md",
                "---\nload: always\n---\n\n# New skill\n",
                "reason",
            )
        planned = json.loads(
            _tool(tools, "write_workspace_doc").forward(
                "NOTES.md", "planned\n", "reason"
            )
        )

    assert planned["status"] == "planned"
    assert not (tmp_path / "NOTES.md").exists()
