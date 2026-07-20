"""Agent-facing tools for querying its own run history (episodic memory).

These read the SQLite run log (``runs.db``) so the agent can answer questions
like "have I done this before?", "what did my last heartbeat do?", or "did I
just fail at this?" from what *actually* happened — distinct from curated
vector memory (facts) and conversation state (continuity).

Privacy: queries are scoped to the current run's context. The configured
``run_log.agent_default_scope`` sets the maximum breadth; the agent may narrow
it per call but never widen beyond it.
"""

from __future__ import annotations

import json
from typing import Optional

from smolagents import tool

from ..constants import clip_text
from ..run_log import RunLogStore

# Breadth ordering: a larger rank sees more history.
_SCOPE_RANK = {"conversation": 0, "team": 1, "all": 2}
_RANK_SCOPE = {v: k for k, v in _SCOPE_RANK.items()}


def _preview(value: Optional[str], n: int = 160) -> str:
    return clip_text(value, n)


def make_run_history_tools(
    store: RunLogStore,
    *,
    current_run_id: Optional[str],
    team_id: Optional[str],
    conversation_id: Optional[str],
    default_scope: str = "team",
    max_results: int = 10,
    max_detail_chars: int = 6000,
) -> list:
    """Build the ``recall_runs`` and ``get_run_detail`` tools for one run."""

    ceiling = _SCOPE_RANK.get(default_scope, 1)

    def _effective_rank(requested: Optional[str]) -> int:
        req = _SCOPE_RANK.get(requested or "", ceiling)
        # The agent may narrow, never widen past the configured ceiling.
        return min(req, ceiling)

    def _scope_kwargs(rank: int) -> dict:
        if rank >= _SCOPE_RANK["all"]:
            return {}
        if rank == _SCOPE_RANK["conversation"] and conversation_id:
            return {"conversation_id": conversation_id}
        # team scope (or conversation scope with no conversation): own team + shared
        return {"team_id": team_id, "include_shared_team": True}

    def _visible(run: dict) -> bool:
        """Whether a specific run is within the agent's allowed breadth."""
        if ceiling >= _SCOPE_RANK["all"]:
            return True
        run_team = run.get("team_id")
        return run_team is None or run_team == team_id

    @tool
    def recall_runs(
        query: Optional[str] = None,
        mode: Optional[str] = None,
        status: Optional[str] = None,
        scope: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> str:
        """Search your own past runs (your episodic history) for compact summaries.

        Use this to check whether you have handled something before, review what
        a recent heartbeat or task did, or find runs that failed. Returns a JSON
        list of run summaries (newest first); call `get_run_detail` with a
        `run_id` to see a run's full step trace.

        Args:
            query: Optional text to match against past tasks and results.
            mode: Optional filter — chat, autonomous, heartbeat, plan, review, or subagent:<name>.
            status: Optional filter — success, error, or cancelled.
            scope: How far to look: "conversation" (this thread), "team" (this team + shared), or "all". Defaults to the configured scope; you can narrow it but not widen it.
            limit: Max results (default configured).
        """
        try:
            rank = _effective_rank(scope)
            n = min(int(limit), 50) if limit else max_results
            rows = store.query_runs(
                grep=query or None,
                mode=mode or None,
                status=status or None,
                exclude_run_id=current_run_id,
                limit=n,
                **_scope_kwargs(rank),
            )
            out = []
            for r in rows:
                tools_used = []
                for step in store.get_run_steps(r["run_id"]):
                    if step.get("tool_calls_json"):
                        try:
                            tools_used.extend(
                                tc.get("name")
                                for tc in json.loads(step["tool_calls_json"])
                            )
                        except Exception:
                            pass
                out.append(
                    {
                        "run_id": r["run_id"],
                        "when": r.get("started_at"),
                        "mode": r.get("mode"),
                        "status": r.get("status"),
                        "duration_s": r.get("duration_s"),
                        "total_tokens": r.get("total_tokens"),
                        "cost_usd": r.get("cost_usd"),
                        "task": _preview(r.get("task")),
                        "result": _preview(r.get("result")),
                        "num_steps": r.get("num_steps"),
                        "tools_used": sorted(set(t for t in tools_used if t)),
                    }
                )
            return json.dumps(
                {"scope": _RANK_SCOPE[rank], "count": len(out), "runs": out}
            )
        except Exception as e:
            return json.dumps({"error": f"recall_runs failed: {e}"})

    @tool
    def get_run_detail(run_id: str) -> str:
        """Get the full step trace of one past run by its `run_id`.

        Returns the run's task, result, usage, and every step (model output,
        tool calls, observations, errors). Use a `run_id` from `recall_runs`.

        Args:
            run_id: The id of the run to inspect.
        """
        try:
            run = store.get_run(run_id)
            if run is None:
                return json.dumps({"error": f"No run found for '{run_id}'."})
            if not _visible(run):
                return json.dumps(
                    {"error": "That run is outside your accessible history scope."}
                )
            budget = max_detail_chars
            steps = []
            for s in store.get_run_steps(run_id):
                obs = s.get("observations")
                if obs and budget >= 0:
                    if len(obs) > budget:
                        obs = obs[:budget] + "…(truncated)"
                    budget -= len(obs)
                elif obs:
                    obs = "…(omitted — detail budget exhausted)"
                tool_calls = []
                if s.get("tool_calls_json"):
                    try:
                        tool_calls = json.loads(s["tool_calls_json"])
                    except Exception:
                        tool_calls = []
                steps.append(
                    {
                        "step": s.get("step_number"),
                        "type": s.get("step_type"),
                        "model_output": s.get("model_output"),
                        "reasoning": s.get("reasoning"),
                        "tool_calls": tool_calls,
                        "observations": obs,
                        "error": s.get("error"),
                    }
                )
            return json.dumps(
                {
                    "run_id": run["run_id"],
                    "when": run.get("started_at"),
                    "mode": run.get("mode"),
                    "status": run.get("status"),
                    "duration_s": run.get("duration_s"),
                    "model": run.get("model"),
                    "total_tokens": run.get("total_tokens"),
                    "cost_usd": run.get("cost_usd"),
                    "task": run.get("task"),
                    "result": run.get("result"),
                    "error": run.get("error_message"),
                    "steps": steps,
                }
            )
        except Exception as e:
            return json.dumps({"error": f"get_run_detail failed: {e}"})

    return [recall_runs, get_run_detail]
