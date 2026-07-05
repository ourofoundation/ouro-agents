# Planning

The planning module turns long-horizon goals into actionable, multi-step
**plans** tied to Ouro **quests**. The agent owns the lifecycle: it
generates plans on a cadence, drives them forward across heartbeats, and
incorporates feedback through a review loop.

Implementation lives in `ouro_agents/modes/planning.py` and the `plan` /
`review` mode profiles. Configuration is under `modes.planning` (hoisted
into top-level `PlanningConfig` at load).

## Data model

```python
class PlanItem:
    id: str           # 8-char id
    description: str
    status: Literal["pending", "in_progress", "done", "skipped"]
    notes: str

class PlanCycle:
    id: str
    status: Literal["planning", "pending_review", "active", "completed", "cancelled"]
    kind: Literal["default", "goal"]
    goal: str
    plan_text: str
    items: list[PlanItem]
    quest_id: str            # the Ouro quest backing this plan
    team_id: str
    heartbeats_completed: int
    human_feedback: str
    revision_count: int
    ...
```

Each team gets its own `PlanStore` under
`workspace/teams/<team_id>/plans/`:

- `active/default.json` — the cadence plan (if any).
- `active/goal-<id>.json` — user-created goal plans.
- `history/<id>.json` — completed/cancelled plans.

Quest items on the platform are the **source of truth** for item status;
the local `PlanCycle` mirrors them. Older post-backed plans without a
`quest_id` are flagged for replan via `needs_replan_stale_active`.

## The cadence loop

When `planning.enabled=true`, the heartbeat scheduler considers running a
planning cycle on its `cadence` (e.g. every 4 hours). The decision logic
(`run_planning_heartbeat`) checks:

- Are we within active hours?
- Are there enough completed heartbeats since the last cycle
  (`min_heartbeats`)?
- Is there an active default plan that isn't complete? (If so, advance
  it instead of replanning.)

Generation flow:

1. Build the planning prompt. Most review material is injected directly:
   the previous cycle's item-level outcome (with unfinished items the
   model must explicitly adopt, park, or drop), a recent-activity digest
   from the run log, work-direction memory, the heartbeat budget derived
   from cadence ÷ heartbeat interval, and the current goal/intent. The
   model can additionally use read-only tools (`search_assets`,
   `get_asset`, `get_comments`, `list_quest_items`) for targeted
   inspection; the only write tool allowed is quest creation/update. The
   prompt also carries an item quality bar: concrete deliverable,
   checkable done-condition, sized to one heartbeat.
2. Call the planning model (typically a stronger model than the
   heartbeat default) under the `plan` mode profile.
3. Post the resulting plan to Ouro as a **quest** in the team's
   workspace.
4. Persist a `PlanCycle` with `status=pending_review`, optionally
   `@`-mention `controller.username` so the controller knows it's ready
   for review.

## Review

While `status=pending_review`, the agent watches for feedback:

- **Webhook-driven**: comment / reply events on the quest are routed by
  `OuroAgent.handle_plan_feedback` → `run_review_heartbeat`.
- **Time-driven**: after `review_window` elapses with no feedback, the
  cycle auto-promotes to `active` (when `auto_approve=true`).
- **Manual**: `ouro-agents review` triggers a review heartbeat
  on-demand. The TUI lets you pick which reviewable plan to inspect.

The review prompt sees the current plan, current quest status, and any
feedback text. It can:

- Refine `plan_text` and individual items.
- Mark items as `in_progress`, `done`, or `skipped` (with `notes`).
- Increment `revision_count`.
- Promote the cycle to `active` once it's been reviewed.

Direction-shaping comments (e.g. "stop tackling X", "always include Y")
are captured by `remember_plan_feedback_direction` so they become durable
`direction` memories that influence future plans.

## Active plans

Once a plan is `active`, normal heartbeat ticks consult the plans index
in their system prompt and may pick up an item to advance. Item status
moves through `pending` → `in_progress` → `done` (or `skipped`); a plan
becomes `completed` when all items are done/skipped.

`format_plans_index_for_prompt` produces a compact listing across all
active plans (across teams when the run isn't team-scoped) so the agent
always sees what's in flight.

## Goal plans

Users can create ad hoc goal plans with `ouro-agents plan "<goal>"`. Goal
plans live alongside the default cadence plan (file naming
`active/goal-<id>.json`) and run in parallel — both show up in the plans
index and either can advance during a heartbeat.

## Failure modes & guards

- A plan whose quest can't be created falls back to writing a local-only
  cycle so the agent can still recover.
- A self-event safety net in the webhook handler prevents the agent's
  own plan posts from triggering immediate review heartbeats.
- `needs_replan_stale_active` triggers regeneration for legacy plans
  without a quest id.

## Manual control

| Command | Effect |
|---------|--------|
| `ouro-agents plan` | Force a planning heartbeat (TUI to pick team). |
| `ouro-agents plan "<goal>" --team-id <id>` | Build a goal plan. |
| `ouro-agents review` | Force a review heartbeat (TUI to pick plan). |

Programmatically:

```python
async with OuroAgent(config) as agent:
    cycle = await agent.force_planning_heartbeat(goal="...", team_id="...")
    reviewed = await agent.force_review_heartbeat(plan_id="...")
```
