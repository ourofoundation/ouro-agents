# Planning

A plan is just an Ouro **quest**. The platform is the single source of truth
for plan content, item status, and lifecycle; there is no local plan mirror.
The agent publishes a new draft quest on a cadence, the quest is approved into
`open` (automatically after the review window, or via a controller comment
on the quest), and its items are then worked through the same quest inbox as
any other quest.

Implementation lives in `ouro_agents/modes/planning.py` (quest creation and
draft auto-approval) and `ouro_agents/modes/heartbeat.py` (the work
inbox and orchestration). Configuration is under `modes.planning` (hoisted
into top-level `PlanningConfig` at load): `enabled`, `model`, `cadence`,
`review_window`, `auto_approve`.

## Local state: one cursor per team

The only local planning state is a tiny cursor at
`workspace/teams/<slug>/planning.json`:

```json
{
  "last_planned_at": "2026-07-07T18:00:00+00:00",
  "last_quest_id": "…",
  "pending_quest_ids": ["…"]
}
```

- `last_planned_at` drives the cadence check.
- `last_quest_id` feeds the previous-plan retrospective into the next
  planning prompt (fetched live from the platform).
- `pending_quest_ids` is the auto-approval worklist: draft quests the
  planning loop itself published.

## Quest lifecycle

```
draft  → open  → closed
(await    (being    (finished
review)   worked)   or shelved)
```

- **Creation** (`run_planning_run`): publishes a fresh, newly-scoped quest
  with `status="draft"`, notifies the controller, and records the cursor.
  Each planning run creates its own quest; unfinished items from earlier
  plans stay on their original quests, which remain open until they resolve.
- **Approval**: with `auto_approve=true`, drafts older than `review_window`
  are promoted to `open` by `auto_approve_due_drafts` at the start of each
  heartbeat. Only cursor-tracked drafts auto-open — a draft someone
  deliberately parked never activates itself. Comments on quests (including
  draft approval, item skips, and plan revisions) use the normal autonomous
  comment path — there is no dedicated review run.

## The heartbeat loop

Every tick (see `_run_heartbeat_impl`):

1. **Load the work inbox** — actionable items across quests assigned to the
   agent plus open quests it owns, assigned items first. Draft quests are
   excluded (awaiting approval); waiting items never enter the inbox.
2. **Planning bookkeeping** — auto-approve due drafts; then, if the inbox is
   empty and a team's cadence is due (and another heartbeat still fits in the
   active window), publish a new draft plan quest for the best team (chosen
   by direction memory and recent trusted activity).
3. **Work the inbox** — pick one item, make one meaningful slice of
   progress, park blocked items with `waiting_on`/`waiting_until`, close the
   quest when its last item completes.
4. **Fallback** — with no inbox and no due planning, run the general
   heartbeat playbook.

A deep inbox naturally defers new plans (work is finished before new quests
appear); quests that stay open only because of parked waiting items never
block planning, because waiting items don't enter the inbox.

## Waiting and recurring items

Quest items carry waiting metadata on the platform (`waiting_on`,
`waiting_until`, `waiting_check_every`). `item_is_waiting` implements the
semantics:

- an unfinished item with a future `waiting_until` (or a `waiting_on` reason
  and no date) is parked and stays out of the inbox;
- a recurring check (`waiting_check_every`) with no next-time is due now;
  when a due recurring item enters a tick, `_advance_due_recurring_items`
  re-parks it one interval out so it polls on a cadence instead of consuming
  every heartbeat. Completing the item stops the recurrence.

## Prompt context

The planning prompt is assembled from live platform data: the previous plan
quest's item-level outcome, a recent-activity digest from the run log,
work-direction memory, and the heartbeat budget (cadence ÷ heartbeat
interval). It carries an item quality bar — concrete deliverable, checkable
done-condition, sized to one heartbeat — and allows read-only inspection
tools. Write tools are `create_quest`, `create_quest_items`, and
`update_quest`: publish once, then fix items/description in place if needed.

Regular runs see a compact index of the agent's own quests (ids and names,
cached briefly) in their system prompt, and can `get_asset` any of them for
detail.

## Manual control

| Command | Effect |
|---------|--------|
| `ouro-agents plan` | Force a planning run (TUI to pick team). |
| `ouro-agents plan "<goal>" --team-id <id>` | Plan around an explicit goal. |

Programmatically:

```python
async with OuroAgent(config) as agent:
    result = await agent.force_planning_heartbeat(goal="...", team_id="...")
```
