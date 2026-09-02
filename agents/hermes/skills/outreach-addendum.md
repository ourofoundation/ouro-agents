---
description: Outreach scars and coil-usage notes that auto-load with the outreach skill
extends: outreach
---

# Outreach addendum: scars and coil notes

These are hard-won lessons that supplement `skills/outreach.md`. The parent
skill has the canonical rules; this file carries the scars that are easy to
miss and the guidance on when to reach for coils vs. manual fallback.

## When to use coils (vs. manual fallback)

Prefer `run_coil` for any standard outreach operation. The four coils enforce
idempotency, immutability guards, and CC policy that manual code routinely
gets wrong:

| Situation | Use | Not |
|---|---|---|
| Start of any outreach tick | `outreach-triage` | hand-written SQL budget queries |
| Before drafting any reply | `read-email-thread` | manual Resend pagination |
| Sending any email | `send-and-log` | separate `send_email` + `update_dataset` calls |
| CRM status update only | `crm-upsert` | raw `update_dataset` upsert |

**Release status (2026-08-23): both `send-and-log` and `crm-upsert` are repaired
and release-checked.** The earlier avoid-until-repaired guidance (broken
`{{table}}` SQL in send-and-log; write-once stripping in crm-upsert) is retired.
The release checkpoint ran both coils through synthetic first-send, follow-up,
and reply transactions in no-send mode and diffed every CRM mutation against the
manual fallback documented in the parent skill: 9/9 mock-adapter cases and 3/3
live read-only dry-run cases passed, with write-once fields byte-identical
through every later transaction, `follow_up_sent` always a lowercase string,
both controller CCs on every send, and deterministic idempotency keys. The
`dry_run: true` path is a zero-side-effect preview. If a coil ever fails again,
fall back to the manual procedure below and record the failure mode here.

`send-and-log` inspects the CRM row before sending. A row without `first_outbound_email_id` is treated as a first send even if the legacy `is_new_contact` hint is false, so pre-created `identified` rows receive the complete first-send fields.

Fall back to manual `run_python` + Resend SDK only when a coil fails or does
not cover the case (e.g. a brand-new email pattern the coil schema doesn't
handle). When you do fall back, still follow the parent skill's idempotency,
CC, and immutability rules exactly.

## CRM scars
- **send-and-log continuation path does not update `status`** (verified by dry-run 2026-08-30): for a CRM row that already has `first_outbound_*` from a bounced prior send, the coil classifies the resend as a continuation and its CRM write touches only last-outbound fields, leaving a `drafted` row `drafted` after a successful send. For bounce-repair first sends, use the manual path and include `status='sent'` in the upsert, or run the coil with `dry_run: true` first and check `first_send_detected`.

- `ouro.datasets.query(...)` returns a pandas DataFrame, not a list of dicts.
  Use `df.iterrows()` and column access, never `.get()` on a dict.
- Upsert batches must be homogeneous: same column set on every row. Omit keys
  you are not changing. Never include `first_outbound_at` or `date_sent` on a
  reply or follow-up upsert.
- `follow_up_sent` must be exactly the lowercase string `"true"` or `"false"`.
  A stray `False`, `True`, or `no` silently drops the row out of every triage
  query.
- When a CRM record shows `follow_up_sent=false` but a follow-up was already
  sent, correct the flag immediately and do not send another follow-up.
- When conducting permanent-magnets outreach, use the correct team URL without
  the `/hermes/` prefix.

## Email sending scars

- Before sending, verify `RESEND_API_KEY` is set in the environment. If it is
  missing, block email sending and require restoration or an alternative
  delivery mechanism.
- On Resend `send_email`, omit unused optional fields (`topicId`, `scheduledAt`,
  etc.). Do not pass the string `"null"` or `""`; both fail validation.
- Always use a deterministic `idempotencyKey` (format in parent skill). Never
  generate a random key per attempt.

## Researcher outreach strategy scars

- Content-driven outreach outperforms pure cold email, but the artifact
  budget is one tick and one quick route run. One quoted number with a
  link beats a multi-day verification quest. If the quick check isn't done in one tick, send the email on the
  strength of specific reading alone, or pick a different target.
- Do not build experiment matrices, protocol notes, deposit templates, or
  multi-claim verification quests for people who have not replied once.
  That is over-investment in silence, not thoroughness. Save deep verification
  for claims you are actively carrying — a live thread, an open quest, a
  community question — or for a controller's direct request; a non-responder's
  paper earns it only after they engage.
- Forward-looking analytical posts outperform documentation of past
  research.
- When pivoting outreach strategy, target computational scientists and ML
  researchers who have published relevant open-source work, inviting them for
  collaboration on defined tasks.

## Standing down a contact

When a controller instructs you to stop contacting someone (handover to a
human, contact asked for human-only, do-not-contact request, etc.), use:

    run_coil("stand-down-contact", {
        "contact": "Name or email",
        "reason": "why we're stopping",
        "handler": "Matt",          # optional: who's taking over
        "permanent": false           # true → do_not_contact status
    })

This looks up the CRM row, updates `status` and `next_action` with a stand-down
note (immutability-safe: never touches `first_outbound_*` or `date_sent`),
and returns a confirmation. Prefer this over manual CRM upsert for any
stand-down instruction.

## Controller-handoff thread guard

- `read-email-thread` must include received messages from Matt (`matt@ouro.foundation`) and Will (`will.bryan421@gmail.com`) that are linked to the contact thread through RFC `Message-ID`, `In-Reply-To`, or `References` headers. Normalized-subject matching is only a clearly labeled fallback for a message with no usable threading headers. Its `controller_reply_detected` / `send_guard` result is a hard stop: immediately stand down the CRM row and do not send a live-thread reply unless Matt, Will, or a controller explicitly directs re-entry. A contact-only inbox filter caused a duplicate calendar invite after Matt had already handled the request.

## Verification-first outreach coil: gate0-verify

Subject to the one-tick / one-route-run artifact budget above: a single
`gate0-verify` call fits that budget; anything beyond it (rebuilds,
follow-up routes, convergence checks) does not.

For verification-first outreach cycles (deep-read a partner paper, verify its
quantitative claims on-platform, then email the authors with receipts), the
verification workflow is packaged as coil `gate0-verify` (published as the
"Gate 0: verify a magnet claim from a CIF" route on hermes-routes, route asset
77d39906-fbea-4a19-aa1e-347f5dec70ee). One call per claim: sanity card v4.1
(embedded in the handler so the published snapshot is self-contained), then the
prediction route matching the claimed property (ALIGNN magmom for
magnetic_ground_state; Curie route for curie_temperature), then a
pre-registered agree/disagree/rejected-input receipt with known limits.

    run_coil("gate0-verify", {"cif_asset_id": "<file-uuid>",
        "claim_property": "magnetic_ground_state", "fm_threshold_ub_per_fu": 0.5,
        "claim_id": "C2", "system_id": "...", "claimed_value": "..."})

Required: `cif_asset_id`. Use `tc_target_K`/`tc_tolerance_K` for Tc claims.
Sanity FAIL returns rejected-input without executing routes; route failures are
reported as errors, never filled in. Negative control for regression:
file 8ba460e9-327d-4c54-a540-31f12c602006 (must be rejected-input).

## Attachments (scar, 2026-08-31)

`send-and-log` has no attachment input: any email that must carry a file (CIFs, PDFs) goes through the manual Resend SDK path in `run_python`. Rules that still bind: deterministic idempotency key, CC Matt + Will, thread headers (`In-Reply-To`/`References`), immediate CRM upsert. On resend-py 2.x the idempotency key is NOT a kwarg on `Emails.send`; pass `options={"idempotency_key": "..."}` as the second argument. If the CRM row's status lagged behind reality (e.g. a reply arrived while the row still read `sent`), triage queues will not surface it — the start-of-tick Resend inbox sweep (`list_received_emails`) is what catches those.

## Stale send-ready drafts (scar, 2026-08-31)

A send-ready draft file is not evidence of send-state. On 2026-08-31 a "refresh" of `drafts/sanvito_cold.md` re-marked it send-ready for 09-01 while the email had actually gone out 2026-08-29 12:32 UTC (Resend 0045f029, delivered, CRM row correctly `sent`). The CRM was right and the draft was wrong; only the file was corrupt, and the planned 09-01 send would have been a duplicate. Before ANY staged send: query Resend history for the recipient address (the draft header never counts), and check the CRM row's `first_outbound_at`. Conversely, the send step must always mark the draft file SENT in place at send time, so the file can never drift from reality. Also: when a canonical draft is refreshed, derived send-path artifacts (e.g. a flattened JSON payload in scratch/) must be regenerated from it or deleted, or they will silently carry the old body.

- `send-and-log` builds its own idempotency key trigger suffix and may normalize a caller-supplied `followup-<first_msg_id>` suffix to `first`. The key stays deterministic under the coil's scheme, so retries stay safe, but if you rely on the exact key format for cross-referencing, read the key from the coil's return value, not from your own input.
