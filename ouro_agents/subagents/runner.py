"""Subagent dispatcher and orchestration utilities.

Provides a unified interface for running subagents:
    run_subagent(profile, task, ctx) -> SubAgentResult

Every subagent runs as a full ToolCallingAgent loop with restricted tools.
No special "pipeline" or "template" modes — just agents with good prompts
and appropriate max_steps.

Plus orchestration helpers:
    run_subagents_parallel — concurrent dispatch of multiple (profile, task) pairs
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from smolagents import ActionStep

from ..artifacts import fetch_asset_content, parse_asset_result
from ..constants import GLOBAL_ORG_UUID
from ..platform_context_prompt import format_platform_context_for_prompt
from ..skills import resolve_skills
from ..tool_prompt import build_tool_calling_system_prompt
from ..usage import format_subagent_usage_summary
from .context import SubAgentContext, SubAgentResult, SubAgentUsage
from .delegate_utils import (
    delegate_success_payload,
    normalize_return_mode,
    resolve_auto_return_mode,
    summarize_delegate_text,
    validate_delegate_result,
)
from .profiles import SubAgentProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Usage tracking helpers
# ---------------------------------------------------------------------------


def _snapshot_tracker(model):
    """Snapshot cumulative tracker state before a subagent run."""
    tracker = getattr(model, "tracker", None)
    if not tracker:
        return None
    return {
        "calls": tracker.num_calls,
        "input_tokens": tracker.total_input_tokens,
        "output_tokens": tracker.total_output_tokens,
        "cached_input_tokens": tracker.total_cached_input_tokens,
        "cache_write_tokens": tracker.total_cache_write_tokens,
        "reasoning_tokens": tracker.total_reasoning_tokens,
        "cost_usd": getattr(tracker, "total_cost_usd", None),
        "input_cost_usd": getattr(tracker, "total_input_cost_usd", None),
        "output_cost_usd": getattr(tracker, "total_output_cost_usd", None),
    }


def _compute_usage(model, before: Optional[dict], wall_ms: int) -> SubAgentUsage:
    """Compute delta usage for subagent, including cost if available."""
    model_id = getattr(model, "model_id", str(model))
    tracker = getattr(model, "tracker", None)
    if not tracker or before is None:
        return SubAgentUsage(model_id=model_id, wall_time_ms=wall_ms)

    def _fdelta(after_v, before_v) -> Optional[float]:
        if after_v is None:
            return None
        if before_v is None:
            return after_v
        return after_v - before_v

    cost_delta = _fdelta(tracker.total_cost_usd, before["cost_usd"])
    in_cost_delta = _fdelta(tracker.total_input_cost_usd, before["input_cost_usd"])
    out_cost_delta = _fdelta(tracker.total_output_cost_usd, before["output_cost_usd"])

    return SubAgentUsage(
        model_id=model_id,
        input_tokens=tracker.total_input_tokens - before["input_tokens"],
        output_tokens=tracker.total_output_tokens - before["output_tokens"],
        cached_input_tokens=tracker.total_cached_input_tokens
        - before["cached_input_tokens"],
        cache_write_tokens=tracker.total_cache_write_tokens
        - before["cache_write_tokens"],
        reasoning_tokens=tracker.total_reasoning_tokens - before["reasoning_tokens"],
        llm_calls=tracker.num_calls - before["calls"],
        wall_time_ms=wall_ms,
        cost_usd=cost_delta,
        input_cost_usd=in_cost_delta,
        output_cost_usd=out_cost_delta,
    )


def _count_agent_steps(agent) -> int:
    return sum(
        1
        for step in getattr(getattr(agent, "memory", None), "steps", [])
        if isinstance(step, ActionStep)
    )


def _format_delegate_payload(
    result: Optional[SubAgentResult],
    profile: Optional[SubAgentProfile],
    subagent: str,
    ctx: SubAgentContext,
    requested_mode: Optional[str] = None,
) -> str:
    mode = normalize_return_mode(
        requested_mode,
        getattr(profile, "default_return_mode", "summary_only"),
    )

    error_payload = validate_delegate_result(result, subagent, mode)
    if error_payload:
        return json.dumps(error_payload)

    assert result is not None
    assert profile is not None

    summary = result.asset_description or summarize_delegate_text(result.text)
    mode = resolve_auto_return_mode(mode, has_asset=bool(result.asset_id))
    return json.dumps(delegate_success_payload(result, subagent, mode, summary))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_subagent(
    profile: SubAgentProfile, task: str, ctx: SubAgentContext
) -> SubAgentResult:
    """Dispatch a subagent. Returns a structured SubAgentResult with usage metrics."""
    logger.info("Running subagent '%s' (max_steps=%d)", profile.name, profile.max_steps)

    before = _snapshot_tracker(ctx.model)
    t0 = time.monotonic()

    try:
        text, agent = _run_agent(profile, task, ctx)
        wall_ms = int((time.monotonic() - t0) * 1000)
        usage = _compute_usage(ctx.model, before, wall_ms)
        usage.steps = _count_agent_steps(agent)

        cost_str = f" cost=${usage.cost_usd:.4f}" if usage.cost_usd is not None else ""
        logger.info(
            "Subagent '%s' usage: %s wall=%dms%s",
            profile.name,
            format_subagent_usage_summary(usage),
            usage.wall_time_ms,
            cost_str,
        )

        if ctx.record_subagent_usage:
            ctx.record_subagent_usage(profile.name, usage)

        asset = parse_asset_result(text)
        if asset:
            return SubAgentResult(
                text=text,
                success=True,
                usage=usage,
                asset_id=asset["asset_id"],
                asset_type=asset["asset_type"],
                asset_name=asset["name"],
                asset_description=asset["description"],
            )
        return SubAgentResult(text=text, success=True, usage=usage)
    except Exception as e:
        wall_ms = int((time.monotonic() - t0) * 1000)
        usage = _compute_usage(ctx.model, before, wall_ms)
        if ctx.record_subagent_usage:
            ctx.record_subagent_usage(profile.name, usage)
        logger.error("Subagent '%s' failed: %s", profile.name, e, exc_info=True)
        return SubAgentResult(
            text="",
            success=False,
            error=str(e),
            usage=usage,
        )


# ---------------------------------------------------------------------------
# Parallel dispatch
# ---------------------------------------------------------------------------


def run_subagents_parallel(
    tasks: list[tuple[SubAgentProfile, str, SubAgentContext]],
    max_workers: int = 4,
) -> list[SubAgentResult]:
    """Run multiple subagents concurrently and return results in input order."""
    if not tasks:
        return []
    if len(tasks) == 1:
        profile, task, ctx = tasks[0]
        return [run_subagent(profile, task, ctx)]

    results: list[Optional[SubAgentResult]] = [None] * len(tasks)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(tasks))) as pool:
        future_to_idx = {
            pool.submit(run_subagent, profile, task, ctx): i
            for i, (profile, task, ctx) in enumerate(tasks)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                logger.error("Parallel subagent %d failed: %s", idx, e)
                results[idx] = SubAgentResult(success=False, error=str(e))

    return [r or SubAgentResult(success=False, error="no result") for r in results]


# ---------------------------------------------------------------------------
# Subagent chaining: build a scoped delegate tool for child subagents
# ---------------------------------------------------------------------------


def _build_chain_delegate(
    profile: SubAgentProfile,
    ctx: SubAgentContext,
    depth: int = 0,
    max_depth: int = 2,
):
    """Build a delegate tool that a subagent can use to invoke other subagents.

    Limited to the profiles listed in ``profile.can_delegate_to`` and
    capped at ``max_depth`` to prevent infinite recursion.
    """
    if not profile.can_delegate_to or depth >= max_depth:
        return None

    from smolagents import tool

    from .profiles import DELEGATABLE_PROFILES

    allowed = {
        name: DELEGATABLE_PROFILES[name]
        for name in profile.can_delegate_to
        if name in DELEGATABLE_PROFILES
    }
    if not allowed:
        return None

    names_str = ", ".join(allowed.keys())

    def _run_one(spec: dict) -> dict:
        sa = spec.get("subagent", "")
        task_str = spec.get("task", "")
        rm = spec.get("return_mode", "")

        child_profile = allowed.get(sa)
        if not child_profile:
            return {
                "status": "error",
                "subagent": sa,
                "return_mode": normalize_return_mode(rm),
                "error": f"Unknown subagent '{sa}'. Available: {names_str}",
            }

        child_ctx = SubAgentContext(
            workspace=ctx.workspace,
            backend=ctx.backend,
            agent_id=ctx.agent_id,
            memory_config=ctx.memory_config,
            model=ctx.model,
            compactor_model=ctx.compactor_model,
            user_id=ctx.user_id,
            conversation_state=ctx.conversation_state,
            conversation_id=ctx.conversation_id,
            run_id=ctx.run_id,
            deferred_tools=ctx.deferred_tools,
            deferred_index=ctx.deferred_index,
            asset_refs=ctx.asset_refs,
            memory_scopes=child_profile.memory_scopes or ctx.memory_scopes,
            ouro_client=ctx.ouro_client,
            record_subagent_usage=ctx.record_subagent_usage,
        )
        result = run_subagent(child_profile, task_str, child_ctx)
        payload = _format_delegate_payload(
            result,
            child_profile,
            sa,
            child_ctx,
            requested_mode=rm,
        )
        return json.loads(payload)

    @tool
    def delegate(tasks: list) -> str:
        """Delegate one or more sub-tasks to specialized subagents. Multiple tasks run in parallel automatically.

        Args:
            tasks: List of task specs. Each is a dict with keys:
                - subagent (str, required): Name of the subagent to invoke.
                - task (str, required): Clear, self-contained description of the sub-task.
                - return_mode (str, optional): summary_only, full_text, or auto. Defaults to the child profile setting.

        Example single:  [{"subagent": "research", "task": "Find info on X"}]
        Example multi:   [{"subagent": "research", "task": "Find info on X"}, {"subagent": "writer", "task": "Draft summary of Y"}]
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not tasks:
            return json.dumps({"status": "error", "error": "No tasks provided."})

        if len(tasks) == 1:
            return json.dumps(_run_one(tasks[0]))

        outputs = [None] * len(tasks)
        with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as pool:
            future_to_idx = {
                pool.submit(_run_one, spec): i for i, spec in enumerate(tasks)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    outputs[idx] = future.result()
                except Exception as e:
                    outputs[idx] = {
                        "status": "error",
                        "subagent": tasks[idx].get("subagent", "?"),
                        "error": str(e),
                    }

        return json.dumps(outputs)

    delegate.description += f"\n\nAvailable: {names_str}"
    return delegate


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------


def _format_task_context(
    task: str,
    ctx: SubAgentContext,
    extra_sections: Optional[list[str]] = None,
) -> str:
    """Build a context string from SubAgentContext for the agent's task."""
    parts: list[str] = []

    asset_context = fetch_asset_content(ctx.deferred_tools, ctx.asset_refs)
    if asset_context:
        parts.append(f"## Input Assets\n{asset_context}")

    platform_text = format_platform_context_for_prompt(ctx.workspace)
    if platform_text:
        parts.append(
            "## Platform context\n"
            f"{platform_text}\n\n"
            "## Ouro asset placement\n"
            "When creating posts, files, or datasets on Ouro, choose the `org_id` and "
            "`team_id` that best fit each artifact from your organizations and teams above. "
            "You may publish different outputs to different teams in the same run when appropriate. "
            "If `agent_can_create` is false for a team, do not use it for API creates — pick another team "
            "or call `get_teams` / `get_organizations` to refresh. "
            "Default visibility: public unless the user or task requires otherwise."
        )
    else:
        parts.append(
            "## Ouro asset placement\n"
            "Platform context cache was empty. Call `get_teams` / `get_organizations` via `load_tool` "
            "to choose `org_id` and `team_id`. If you need an org id before loading teams, use the "
            f"global organization id `{GLOBAL_ORG_UUID}`. "
            "Default visibility: public unless the user or task requires otherwise."
        )

    if ctx.conversation_state:
        parts.append(
            f"## Conversation State\n{ctx.conversation_state.format_for_prompt()}"
        )

    if extra_sections:
        parts.extend(section for section in extra_sections if section)

    parts.append(f"## Task\n{task}")
    return "\n\n".join(parts)


def _run_agent(
    profile: SubAgentProfile, task: str, ctx: SubAgentContext
) -> tuple[str, object]:
    """Run a subagent as a ToolCallingAgent with restricted tools."""
    from ..memory.tools import make_memory_tools
    from ..tools.agent_base import (
        SanitizedToolCallingAgent as _SanitizedToolCallingAgent,
    )

    tools: list = []
    active_deferred_index: list[dict] = []
    preloaded_raw_names: list[str] = []

    memory_tool_names = {"memory_recall", "memory_status"}
    if memory_tool_names & set(profile.allowed_tools):
        mem_tools = make_memory_tools(
            ctx.backend,
            ctx.agent_id,
            user_id=ctx.user_id,
            workspace=ctx.workspace,
        )
        allowed = set(profile.allowed_tools)
        tools.extend(t for t in mem_tools if t.name in allowed)

    agent_ref: dict = {}
    if profile.can_load_mcp_tools and ctx.deferred_tools:
        deferred_tools = ctx.deferred_tools
        active_deferred_index = ctx.deferred_index

        if profile.allowed_servers:
            allowed_servers = set(profile.allowed_servers)
            active_deferred_index = [
                item
                for item in active_deferred_index
                if item["server"] in allowed_servers
            ]
            allowed_names = {item["tool"] for item in active_deferred_index}
            deferred_tools = {
                k: v for k, v in deferred_tools.items() if k in allowed_names
            }

        from ..tools.mcp_tools import make_load_tool

        load_tool = make_load_tool(deferred_tools, active_deferred_index, agent_ref)
        tools.append(load_tool)

    # Preload MCP tools specified by the profile
    for qualified_name in profile.preload_tools:
        tool_obj = ctx.deferred_tools.get(qualified_name)
        if tool_obj:
            tools.append(tool_obj)
            item = next(
                (
                    entry
                    for entry in ctx.deferred_index
                    if entry["tool"] == qualified_name
                ),
                None,
            )
            preloaded_raw_names.append(
                item["raw_name"] if item else qualified_name.split(":")[-1]
            )
            logger.info(
                "Preloaded tool '%s' for subagent '%s'", qualified_name, profile.name
            )

    if profile.needs_python_tool:
        from ..tools.python_tool import make_python_tool

        python_tool, _executor = make_python_tool(
            workspace=ctx.workspace,
            ouro_client=ctx.ouro_client,
        )
        tools.append(python_tool)

    chain_delegate = _build_chain_delegate(profile, ctx)
    if chain_delegate:
        tools.append(chain_delegate)

    if not tools:
        logger.warning(
            "Subagent '%s' has no tools — running with final_answer only",
            profile.name,
        )

    from ..display import create_subagent_logger, get_display

    _display = get_display()
    subagent_logger = create_subagent_logger(
        profile.subagent_log_level,
        _display,
    )

    agent = _SanitizedToolCallingAgent(
        tools=tools,
        model=ctx.model,
        compactor_model=ctx.compactor_model,
        max_steps=profile.max_steps,
        logger=subagent_logger,
        is_chat_mode=False,
    )
    agent_ref["agent"] = agent

    if profile.system_prompt:
        agent.prompt_templates["system_prompt"] = build_tool_calling_system_prompt(
            profile.system_prompt
        )
    else:
        agent.prompt_templates["system_prompt"] = build_tool_calling_system_prompt()

    task_sections: list[str] = []

    if profile.skills:
        skill_bodies = resolve_skills(profile.skills, workspace=ctx.workspace)
        if skill_bodies:
            task_sections.append("## Skills\n" + "\n\n---\n\n".join(skill_bodies))

    if preloaded_raw_names:
        task_sections.append(
            "## Preloaded Tools\n"
            f"These tools are already loaded and ready to call directly: "
            f"{', '.join(f'`{name}`' for name in preloaded_raw_names)}. "
            "Do not call `load_tool` for them."
        )

    if profile.can_load_mcp_tools and active_deferred_index:
        task_sections.append(
            "## MCP Tool Rules\n"
            "- Emit actual tool calls only. Do not narrate tool usage or write pseudo-calls.\n"
            "- If a tool is not preloaded, call `load_tool` first using a name from the directory below.\n"
            "- After `load_tool`, call the tool by the returned `call_as` name.\n"
            "- If a tool call fails, retry once with corrected arguments instead of describing the retry."
        )
        directory = "\n".join(
            f"- {item['tool']}: {item['description'][:240]}"
            for item in active_deferred_index
        )
        task_sections.append(
            f"## Available Tools (use load_tool to activate)\n{directory}"
        )

    if chain_delegate:
        task_sections.append(
            f"## Delegation\nYou can delegate sub-tasks to: "
            f"{', '.join(profile.can_delegate_to)}. "
            f"Use `delegate` with a list of task specs — multiple tasks run in parallel. "
            f"By default it returns a compact JSON handoff with summary and asset metadata; "
            f'use `return_mode: "full_text"` only when you truly need the full body.'
        )

    effective_task = _format_task_context(task, ctx, task_sections)

    try:
        result = agent.run(effective_task)
        return str(result), agent
    except Exception as e:
        logger.error("Subagent '%s' agent loop failed: %s", profile.name, e)
        return "", agent
