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
