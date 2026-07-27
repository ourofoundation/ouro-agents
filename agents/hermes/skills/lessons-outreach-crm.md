---
description: Learned lessons about outreach-crm (distilled from memory)
load: stub
---

# Lessons: outreach-crm

- Resend is authoritative for email history and daily budget counts. Never count today's outbound with CRM `date_sent = today` — that field is first-send-only and was overwritten on replies in the past.
- CRM stores contact state. Canonical send fields: `first_outbound_at` / `first_outbound_email_id` (write-once), `last_outbound_at` / `last_outbound_email_id`, `last_inbound_at` / `last_inbound_email_id`. Legacy `date_sent` / `email_id` are compatibility only — set on first send, never overwrite on replies.
- Always update the outreach tracker after every email send or reply with contact name, email, status, follow_up_sent flag, last-outbound/inbound fields, and next_action.
- Always CC `matt@ouro.foundation` on every outreach email send and follow-up (Resend `cc` field) so Matt can add color or corrections on the live thread.
- Before drafting any email to a contact who has already been emailed, re-read the full Resend thread in **both** directions (`list_received_emails` + `get_received_email` **and** `list_emails` + `get_email`). CRM `reply_received` / `next_action` are lossy and go stale; composing from them alone re-asks settled questions (e.g. Prasanna had already agreed to include Liqin) and re-answers ones you already closed (e.g. Jack Evans licensing reply sent twice).
- If Resend already shows a recent Hermes outbound that answered the open questions on the same subject, do not send again — update `next_action` and wait.
- After reading a new reply, immediately upsert `reply_received` / `last_inbound_*` with what they actually said and narrow `next_action` to only unsettled items — never leave the CRM reflecting your outbound framing once they have answered.
- Every `send_email` must pass a deterministic `idempotencyKey` = `hermes:{contact_id}:{intent}:{trigger}`. Retries reuse the same key; a different email needs a new key.
- When managing CRM for sponsors, ensure follow-ups are sent within 14 days of `first_outbound_at`.
- When conducting permanent-magnets outreach, always use the correct team URL without the `/hermes/` prefix.
- `ouro.datasets.query(...)` returns a pandas DataFrame — use `df.iterrows()` / column filters, never assume list[dict] with `.get()`.
- Parse CRM `first_outbound_at` as ISO-8601; fall back to `date.fromisoformat(str(date_sent))` only when `first_outbound_at` is null. Do not use `datetime.fromisoformat` on bare `date_sent` dates alone for budget math.
- On Resend `send_email`, omit unused optional fields (`topicId`, `scheduledAt`, etc.) — do not pass the string `"null"` or `""`; both fail validation.
- Upsert batches must be homogeneous (same column set on every row). Omit keys you are not changing; never include `first_outbound_at` / `date_sent` on a reply upsert.
