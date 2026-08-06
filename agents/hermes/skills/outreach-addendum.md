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

`send-and-log` inspects the CRM row before sending. A row without `first_outbound_email_id` is treated as a first send even if the legacy `is_new_contact` hint is false, so pre-created `identified` rows receive the complete first-send fields.

Fall back to manual `run_python` + Resend SDK only when a coil fails or does
not cover the case (e.g. a brand-new email pattern the coil schema doesn't
handle). When you do fall back, still follow the parent skill's idempotency,
CC, and immutability rules exactly.

## CRM scars

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

- Read external papers, run predictions on their systems, and publish the
  comparison as genuine value before drafting outreach emails to the authors.
- Prefer content-driven inbound over pure cold email: read a recent
  high-impact paper, run fresh analysis using Ouro routes (property
  prediction, hull energy, structure relaxation), post the findings publicly,
  then reach out with the new angle. Forward-looking analytical posts
  outperform documentation of past research.
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
