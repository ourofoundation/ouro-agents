---
description: Learned lessons about outreach-crm (distilled from memory)
load: stub
---

# Lessons: outreach-crm

Canonical CRM / Resend / send rules live in `skills/outreach.md`. Keep only scars that are easy to miss:

- When conducting permanent-magnets outreach, always use the correct team URL without the `/hermes/` prefix.
- `ouro.datasets.query(...)` returns a pandas DataFrame — use `df.iterrows()` / column filters, never assume list[dict] with `.get()`.
- On Resend `send_email`, omit unused optional fields (`topicId`, `scheduledAt`, etc.) — do not pass the string `"null"` or `""`; both fail validation.
- Upsert batches must be homogeneous (same column set on every row). Omit keys you are not changing; never include `first_outbound_at` / `date_sent` on a reply upsert.
