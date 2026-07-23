---
description: Learned lessons about outreach-crm (distilled from memory)
load: stub
---

# Lessons: outreach-crm

- Always update the outreach tracker after every email send or reply with contact name, email, send date, status, follow_up_sent flag, and next_action.
- Always CC `matt@ouro.foundation` on every outreach email send and follow-up (Resend `cc` field) so Matt can add color or corrections on the live thread.
- Before drafting any email to a contact who has already replied, re-read the full Resend inbox thread (`list-received-emails` + `get-received-email`). CRM `reply_received` / `next_action` are lossy and go stale; composing from them alone re-asks settled questions (e.g. Prasanna had already agreed to include Liqin, and a later prep email asked again).
- After reading a new reply, immediately upsert `reply_received` with what they actually said and narrow `next_action` to only unsettled items — never leave the CRM reflecting your outbound framing once they have answered.
- When managing CRM for sponsors, ensure follow-ups are sent within 14 days.
- When conducting permanent-magnets outreach, always use the correct team URL without the `/hermes/` prefix.
- `ouro.datasets.query(...)` returns a pandas DataFrame — use `df.iterrows()` / column filters, never assume list[dict] with `.get()`.
- Parse CRM `date_sent` with `date.fromisoformat(str(value))`, not `datetime.fromisoformat`.
- On Resend `send_email`, omit unused optional fields (`topicId`, `scheduledAt`, etc.) — do not pass the string `"null"` or `""`; both fail validation.
