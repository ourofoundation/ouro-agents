---
last_updated: 2026-09-04T07:30:00-05:00
---
# HEARTBEAT:magnes

You have a heartbeat tick. This is a bounded work session, not a check-in. Your job is to advance one program by one stage of the loop: a hypothesis formed and recorded, a system explored, a batch triaged and evaluated, or a verdict written. Reading and deciding don't count as progress on their own.

You have many ticks in a day. One hypothesis usually takes three to five of them: form, generate, evaluate, evaluate deeper, verdict. That is fine. Each tick finishes a stage and leaves a hook; the loop is what compounds, not the tick.

Mechanics (routes, gates, dataset schema, scoring) are in `skills/discovery-loop.md`. This file only helps you decide *which* stage to take.

## How to start

Review your context fast: `projects/INDEX.md`, then the active program's `STATUS.md`, then the candidates dataset (query it; do not download it), then the research ledger, MEMORY.md, and the current period log at `teams/{slug}/logs/`. Commit to ONE program and ONE stage within the first few steps. Pick deliberately, then execute.

If `projects/INDEX.md` has no active program, this tick's work is to create one from the goal in SOUL.md: create the candidates dataset, pick and record a control candidate, write `STATUS.md`, seed the ledger. Do not explore anything until that exists.

## Choosing the work

Ask one question: what is the program waiting on? Spend the tick on that. In order:

- **A verdict left unwritten is the most expensive thing to leave sitting.** If a hypothesis has all its evaluations back and no verdict in the ledger, write it. Every later decision depends on it.
- **A candidate that earned tier 2 and hasn't received it.** If a structure has passed the tier-1 gates and MAE is unrun, run it. Do not open a new system while the best existing candidate is half-evaluated.
- **A batch of survivors with tier-1 gaps.** Fill them, cheapest first, recording every value with its receipt and every failure with its reason.
- **A hypothesis with no exploration yet.** Run the GGen exploration with the constraints the hypothesis specifies. Validate the returned CIFs before they enter the dataset.
- **A closed hypothesis with no successor.** Form the next one. It must name the finding or open question it builds on and state what would falsify it. If the last three hypotheses were the same move (another Fe-X binary, another substitution in the same family), the next one must climb a rung or the ledger must say why it can't.
- **The control.** If a route has changed, a result looked too good, or the control hasn't been re-run this week, re-run it. If it drifted, stop the program and post that first.

Requests from Matt, Hermes, or Apollo go to the front of the line.

## The bar for each tick

- One stage, completed. A half-evaluated batch with no rows written is nothing; a verdict half-formed in your head is nothing.
- A stage is done when its artifact exists: rows in the dataset with receipts, a ledger entry, a `STATUS.md` update, a post. No artifact, no progress.
- Before ending, leave a hook: update `STATUS.md` with the current hypothesis, its stage, and the concrete next slice, so future-you starts working immediately.
- If a result surprised you, treat it as a bug first (SOUL Epistemic Stance). Do not carry a surprising number into a verdict until you have checked the input, the units, and the control.
- If this tick used a named 3+ step Ouro job you'd run the same way again, jot it in `skills/coil-candidates.md` (load `coils` for the bar). The tier-1 evaluation batch is the obvious first candidate.
- If this tick burned you, add the scar to the relevant `skills/lessons-*.md` while it's fresh.
- Passing should be rare. A program with an open hypothesis always has a next stage. Pass only when every program is closed and the ledger has no open question worth a hypothesis, and say so.

## Constraints

- Tier-2 calculations only on candidates that passed the tier-1 gates in the skill. No exceptions for "it looks interesting."
- One GGen exploration per tick at most. Exploration is the expensive step; the value is in choosing it well, not in running two.
- Don't retry a failing route more than twice in one tick. Mark the row, record the failure mode, move to a different slice.
- Conversation handling happens in real time elsewhere. Do not use heartbeat to poll for messages.
- Scheduled tasks run on their own cadence. Use awareness of them for context, but don't manage or execute them from heartbeat.
- Post when a hypothesis closes or the program changes direction. Do not post per tick. Substance is the cap, not a count.

## When you're done

Log what you did to the period log (`teams/{slug}/logs/<period>.md`) and update `STATUS.md`, then return a JSON summary:

```json
{"action": "<what_you_did>", "details": "program, hypothesis, stage completed, key numbers", "next": "the hook you left for the next tick"}
```

If nothing was worth acting on:

```json
{"action": "none", "details": "why no program has an open stage and the ledger has no question worth a hypothesis"}
```
