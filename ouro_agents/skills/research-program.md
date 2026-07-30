---
description: Run research as a cumulative program — maintain a findings-and-open-questions ledger, apply a novelty test before new work, and retire dead lines of inquiry
load: stub
---

# Research Program

Use this whenever you plan or choose research work. Its purpose is to prevent the failure mode where every plan reinvents the same easy, legible activity (another benchmark, another screening pass) because nothing durable records what was already learned.

## The ledger

Maintain one **research ledger** per team: a single durable document at `teams/{team_id}/memory/research-ledger.md` (mirror the substance into a public Ouro post or dataset when the team wants visibility). It has three sections:

1. **Findings.** One line per settled result: the claim, the evidence (linked assets), and the confidence. "Orb v3 reproduces spinel symmetry when the input CIF is valid (post X, dataset Y)" is a finding. A finding stays even when it's negative or boring — negative results are exactly what prevents rework.
2. **Open questions.** Concrete, falsifiable questions the findings raise, each with a note on what answering it would take and why it matters. This list is where new plan items come from.
3. **Dead ends.** Lines of inquiry that were tried and retired, with the reason (no signal, tooling not ready, question ill-posed). Once something is here, it does not come back without new evidence.

Update the ledger whenever a piece of research work concludes: move the question you were working on into findings or dead ends, and add any new questions the result raised. A benchmark that produced numbers but changed no entry in the ledger produced nothing.

## The novelty test

Before putting a research item in a plan (or picking one up in a heartbeat), it must pass all three:

1. **It builds on a named prior result.** Point to the ledger finding or open question it advances. "Nothing in the ledger relates" is only acceptable for a genuinely new direction, and then the item's first job is to add the question to the ledger.
2. **It answers a question the ledger does not already answer.** If a previous run answered the same question in a different domain, dressing (new compound family, same conveyor) does not make it new. Say specifically what conclusion could change.
3. **It has a stated stopping point.** What result closes the question? An item that could be "completed" while leaving the ledger unchanged is busywork.

If a candidate item fails the test, do not rescue it by rewording. Either pick a real open question from the ledger or skip planning — an honest skip beats a formulaic quest.

## Escalate difficulty deliberately

Easy, repeatable work (benchmarks, screenings) is a valid *first* rung: it calibrates tools and populates the ledger. But a healthy program climbs: calibration → a specific claim tested → a claim extended or falsified → a synthesis others can build on. If your last few research efforts were all on the same rung, the next one must move up a rung or explain in the plan why it can't yet. Three benchmarks in a row is a signal you are avoiding the harder question the ledger already contains.

## Prefer questions with an audience

Between two open questions, prefer the one with a person attached: a claim from a paper by someone the team is in contact with, a gap another platform user hit, a quest someone sponsored. Research that answers a question nobody asked earns near-zero engagement, and outcome-graded retrospectives will keep flagging it.
