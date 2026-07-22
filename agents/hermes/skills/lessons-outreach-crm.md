---
description: Learned lessons about outreach-crm (distilled from memory)
load: stub
---

# Lessons: outreach-crm

- Always update the outreach tracker after every email send or reply with contact name, email, send date, status, follow_up_sent flag, and next_action.
- Always CC `matt@ouro.foundation` on every outreach email send and follow-up (Resend `cc` field) so Matt can add color or corrections on the live thread.
- When managing CRM for sponsors, ensure follow-ups are sent within 14 days.
- When conducting permanent-magnets outreach, always use the correct team URL without the `/hermes/` prefix.
- `ouro.datasets.query(...)` returns a pandas DataFrame — use `df.iterrows()` / column filters, never assume list[dict] with `.get()`.
- Parse CRM `date_sent` with `date.fromisoformat(str(value))`, not `datetime.fromisoformat`.
- On Resend `send_email`, omit unused optional fields (`topicId`, `scheduledAt`, etc.) — do not pass the string `"null"` or `""`; both fail validation.
