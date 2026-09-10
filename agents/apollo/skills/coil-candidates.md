---
description: Jobs worth turning into coils — you maintain this list
load: stub
---

# Coil candidates

You own this file. Nothing auto-writes it. Add an entry when you notice a
**job** you would run the same way again. Prune when you author the coil or
realize it was a one-off. Keep the list short.

A candidate is a named workflow with a stable purpose. Repeated searches,
polling, and calling the same tool on different IDs are not candidates.
Load the `coils` skill for the contract and templates.

## Template

### short-name

- **Job:** one sentence — what this does and when you need it
- **Steps:** the sequence; mark what varies vs what's fixed
- **Why a coil:** errors avoided, invariants enforced, or steps you keep redoing
- **Seen:** date you noticed it

## Candidates

### mention-triage

- **Job:** Read the full text of specific comments during notification triage — needed every inbox pass because `get_comments` cannot fetch a comment by ID and API `description.text` is truncated (~330 chars).
- **Steps:** (1) `get_notifications` -> collect the (comment) `asset.id` per mention; (2) SDK `ouro.comments.retrieve(id)` per id; (3) extract text by walking the rich-doc `content` dict (text nodes + paragraph breaks), never `description.text`. Varies: id list; fixed: fetch + extraction + compact print (id, author, team, parent, text).
- **Why a coil:** the truncation trap and content-doc walk cost two failed parses to discover; a coil enforces "full text, not preview" and returns one compact triage payload.
- **Seen:** 2026-09-02
- Re-seen 2026-09-09: hand-rolled the same retrieve+content-walk again during inbox triage (second time) — author next quiet tick.
- 2026-09-03 — Apollo quarterly/known-answer service-maintenance suite: execute 8 control cases across Energy Gate/CIF Analyzer/Robocrys/SMACT routes, compare vs baselines, write dataset + per-service records. Same 8 executes + 1 dataset every cadence; good coil.

## CIF simplified-topology identifier (used 2026-09-03)
- Trigger: user pastes a simplified CIF + connection windows in a conversation.
- Steps: parse symops/sites -> expand to cell -> shell-counted bond lists -> physical
  cluster/triangle classes with cell offsets -> quotient graphs -> validated topology
  engine (point symbol, vertex rings, CS, components, chirality) -> reply in thread.
- Why a coil: repeatable for this user's chemistry questions; engine + reference controls
  already coded (scratch/topo-nanziang/). Must keep the control suite (pcu/bcu/dia/fcu/srs).

### dataset-row-anomaly-audit

- **Job:** When a curated screening dataset row (e.g. Oliynyk magnet dataset 019f5902) shows an anomalous computed value, validate its input CIF and re-derive the suspect columns through the owning routes, then hand the owner corrected candidate values. Ran identically twice in two days (tau-MnAl L10 row 2026-09-05 am, tau-MnAl (tau) row 2026-09-05 pm).
- **Steps:** (1) `query_dataset` the row (ILIKE filter on compound; schema first — no guessing column names, server 400s); (2) `download_asset` the row's cif_file_id; (3) run the structure-validation checklist in sandbox (parse, composition, spglib at symprec 0.01/0.1, min pair distance, density, occupancies); (4) if input valid but values implausible, re-derive via the relevant route (MP e_hull route 75fe7f4b with input_assets={"structure": <file-id>}; note allow_unrelaxed default and that e_hull==E_form identical values = degenerate-reference signature); (5) comment on the dataset with validated-input status + fresh values + action link + recommendation, mentioning the dataset owner (row upserts 403 for Apollo — owner applies). Varies: dataset/row/CIF id, which route owns the column; fixed: validation checklist, degenerate-signature check, receipt-chained comment.
- **Why a coil:** the checklist + signature + route order is exactly what caught the April broken-CIF incident and this artifact row; enforcing "validate input before claiming values wrong" prevents both false alarms and missed contamination. Receipt chain makes the owner's fix two minutes.
- **Seen:** 2026-09-05

## mae-control-runner (added 2026-09-08)
Spawn a long MAE job against the deployed apollo-large-cell-mae app (Function.from_name().spawn — never ephemeral modal run), poll get_call_graph to completion, fetch the receipt from the results volume/file asset, compare MAE against the acceptance bar, and append the comparison row to the maintenance dataset. Will run 3+ times (FeW L1_0 r2 verification, Fe3W-motif control, Fe17W3 + k-spacing bracket arms) with identical shape; ideal tier-1 coil once the first receipt exists to define the parse.
