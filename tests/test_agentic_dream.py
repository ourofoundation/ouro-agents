from pathlib import Path
from types import SimpleNamespace

from ouro_agents.config import DreamConfig, MemoryConfig
from ouro_agents.memory import agentic_dream
from ouro_agents.memory.dream_git import DreamSnapshot
from ouro_agents.modes.profiles import RunMode


class _RunLog:
    def query_runs(self, **kwargs):
        return [
            {
                "run_id": "run-1",
                "started_at": "2026-09-01T10:00:00+00:00",
                "mode": "heartbeat",
                "status": "success",
                "num_steps": 4,
                "task": "Do useful work",
                "result": "Done",
            }
        ]


class _Memory:
    def get_all(self, **kwargs):
        return []


class _Agent:
    def __init__(self, workspace: Path):
        self.config = SimpleNamespace(
            agent=SimpleNamespace(
                workspace=workspace,
                name="hermes",
                model="strong-model",
            ),
            dream=DreamConfig(dry_run=True),
            memory=MemoryConfig(extraction_model="extract", embedder="embed"),
        )
        self.doc_store = None
        self.memory = _Memory()
        self._run_log = _RunLog()
        self.run_kwargs = None

    def _utility_model_id(self):
        return "utility-model"

    def _model_id_for_role(self, role):
        return "dream-model" if role == "dream" else None

    def _build_model(self, model_id, **kwargs):
        return SimpleNamespace(model_id=model_id)

    async def run(self, task, **kwargs):
        self.run_kwargs = kwargs
        report = next(
            item for item in kwargs["extra_tools"] if item.name == "write_dream_report"
        )
        report.forward(
            "# Graded previous dreams\n\nNo prior dream.\n",
            [],
            ["No repeated task next window."],
        )
        return "Dream complete."


def test_dream_runs_normal_mode_and_writes_audit(tmp_path: Path, monkeypatch):
    agent = _Agent(tmp_path)
    current = DreamSnapshot(kind="git", ref="abc", changed=False)
    monkeypatch.setattr(agentic_dream, "_current_git_snapshot", lambda workspace: current)
    monkeypatch.setattr(
        agentic_dream,
        "run_refinement_phase",
        lambda agent, dry_run=False: {"pending": 0, "edits": 0},
    )
    monkeypatch.setattr(
        agentic_dream,
        "compact_memory_md",
        lambda *args, **kwargs: False,
    )

    summary = agentic_dream.run_dream(agent, dry_run=True)

    assert agent.run_kwargs["mode"] == RunMode.DREAM
    assert agent.run_kwargs["skip_memory"] is True
    assert agent.run_kwargs["run_id_override"] == summary["run_id"]
    assert summary["dry_run"] is True
    assert summary["window"]["new_runs"] == 1
    assert Path(summary["report_path"]).exists()
    assert Path(summary["audit_log"]).exists()
    assert summary["expectations"] == ["No repeated task next window."]
