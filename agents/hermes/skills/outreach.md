---
description: Concrete mechanics for researcher/sponsor outreach — the CRM dataset, dedup before sending, follow-up cadence, and reply handling
load: always
---

# Outreach

This is the operational layer under your SOUL. SOUL tells you *why* and *what
bar*; this tells you *exactly how* to run outreach — including every numeric
cap — without spamming, losing track of anyone, or guessing. HEARTBEAT
orchestrates *which* slice to take on a tick; it does not redefine these rules.

**Sources of truth:**
- **Resend** is authoritative for email history (what was sent, what was
  received, when, and message ids). Daily email budgets, duplicate detection,
  and thread reconstruction always come from Resend.
- **The CRM dataset** is the durable contact-state index (who, status, next
  action, first/last timestamps). It is *not* the full thread and *not* the
  send ledger. Scratch JSON files in the workspace are never authoritative.

**Outreach coils (prefer these):** four private coils under `coils/` compress
the repeated CRM + Resend sequences. Call them with `run_coil` — do **not**
hand-compose the equivalent `run_python` SQL / Resend MCP steps on a normal
heartbeat when a coil covers the job.

| When | Coil | Call |
|---|---|---|
| Start of tick: budget + queues | `outreach-triage` | `run_coil("outreach-triage", {})` |
| Before any reply / continuation | `read-email-thread` | `run_coil("read-email-thread", {"email": "..."})` |
| Send + CRM log in one step | `send-and-log` | `run_coil("send-and-log", {to, subject, html, contact_id, intent, …})` |
| CRM-only update (immutability guard) | `crm-upsert` | `run_coil("crm-upsert", {"contact_id": "…", "updates": {…}})` |

The sections below document caps, schemas, and fallbacks. Use them to
understand *what* the coils enforce and to repair a coil — not as the default
tick path. Fall back to Resend MCP / `run_python` only when a coil fails or
does not cover the case.

**Sandbox:** Docker has the `resend` Python package and `RESEND_API_KEY` /
`RESEND_SENDER` (`hermes@agents.ouro.foundation`). Inside `run_python` /
`run_coil` handlers use the SDK (not MCP):

```python
import os
import resend

resend.api_key = os.environ["RESEND_API_KEY"]
email = resend.Emails.send({
    "from": os.environ["RESEND_SENDER"],
    "to": ["them@example.edu"],
    "subject": "...",
    "html": "...",
})
```

## Daily caps (canonical)

These numbers live here only. Do not invent alternate caps from memory or
period logs.

| Cap | Limit |
|---|---|
| Outbound emails / day (cold + follow-up + live-thread reply) | **8** total |
| Cold / first contacts / day | **4** |
| Follow-ups / day | **4** |
| Same artifact as the "something new" in follow-ups / day | **2** (so one artifact never headlines more than one day's follow-ups) |

At 8 outbound for the day, the only email still allowed is a live-thread reply
that would otherwise sit a full day — and it still counts. Cold sends and
follow-ups never exceed the total cap.

Batch cold sends create batch follow-up waves a week later; spreading sends
spreads follow-ups. Respect stop requests immediately and permanently.

## One quest per research group

Scope every plan/quest to a **single research group's outreach cycle** — the
one paper and its authors you are contacting. A quest's items are that group's
full pipeline end to end: paper selection, deep-read, CIF generation,
prediction routes, analysis post, personalized email, CRM logging, and the one
allowed follow-up. When that group is done (including its follow-up), the quest
closes.

Do **not** grow one quest into a running log of many groups. When you start a
new group, that is a **new quest**, even if a prior group's quest is still open
waiting on a reply, a draft review, or a scheduled follow-up. Several quests
being open at once is expected and good — each stays small, scoped, and
reviewable. If a previous cycle left unfinished items, they remain tracked on
their own quest and you keep advancing them there; never copy them into the new
group's quest.

## The CRM dataset

Your outreach record is a single Ouro dataset. Treat it as a lightweight CRM —
one row per person you have ever contacted.

```
DATASET_ID = 019ee292-8c6c-7038-81c4-46eb601c31b6
```

One row per contact. Columns:

| column | meaning |
|---|---|
| `id` | Stable row key (UUID). Set it yourself on insert; reuse it forever to update that contact. This is the key `upsert` merges on. |
| `name` | Person (or org, for sponsors with no named contact). |
| `type` | `researcher` or `sponsor`. |
| `institution` | Lab, university, company, fund, or program. |
| `email` | Best professional address. `n-a` if none found yet. |
| `focus` | What they actually work on. Specific enough to write a real email from. |
| `batch` | Loose grouping/campaign tag (e.g. `2d-magnets-1`, `sponsor-1`, `ml-materials`). |
| `first_outbound_at` | ISO-8601 UTC timestamp of the **first** outbound email to this contact. Set once; never overwrite. |
| `first_outbound_email_id` | Resend id of that first outbound. Set once; never overwrite. |
| `last_outbound_at` | ISO-8601 UTC timestamp of the most recent outbound. Update on every send. |
| `last_outbound_email_id` | Resend id of the most recent outbound. Update on every send. |
| `last_inbound_at` | ISO-8601 UTC timestamp of the most recent inbound from them. Update when you read a new reply. |
| `last_inbound_email_id` | Resend id of that inbound. Update when you read a new reply. |
| `status` | Lifecycle, see below. |
| `reply_received` | `false`, or a short note summarizing what they actually said. |
| `follow_up_sent` | `false` or `true`. You get exactly one cold follow-up. |
| `next_action` | The concrete hook for future-you: what to do and roughly when. |
| `date_sent` | **Deprecated.** Legacy ISO date of first send (`n-a` until sent). Keep writing it on first send for old queries, but never overwrite it on replies, and never use it for daily budget counts. Prefer `first_outbound_at`. |
| `email_id` | **Deprecated.** Legacy "latest" Resend id. Prefer `last_outbound_email_id`. Do not treat this as first-send proof. |

`follow_up_sent` must be exactly the lowercase string `true` or `false` —
every triage query matches on those literals, and a stray `False`/`no` drops
the row out of every queue. `reply_received` starts as `false` and becomes a
short note once they reply (any value other than `false` reads as "replied").

**Immutability rule:** `first_outbound_at` and `first_outbound_email_id` are
write-once. On a reply or follow-up upsert, omit them entirely. Overwriting
first-send fields is how follow-up windows and history get corrupted.

### Status lifecycle

`identified` -> `drafted` -> `sent` -> `replied` (terminal-ish) or `blocked`
(also: `no_reply`, `do_not_contact`).

- `identified`: a real target with a reason to contact, no email written yet.
- `drafted`: email written, not sent (waiting on a channel, a warm intro, etc.).
- `sent`: email delivered.
- `replied`: they answered. Put the substance in `reply_received` and the next
  move in `next_action`.
- `blocked`: cannot proceed (address bounced, suppressed by Resend, no public
  contact). Record *why* and any fallback path in `next_action`.
- `no_reply`: follow-up window passed in silence; leave them alone.
- `do_not_contact`: they asked to stop, or a controller directed no further
  contact. Permanent.

## Read before you act

Start of every outreach tick: **`run_coil("outreach-triage", {})`** for budget
+ queues. Do not work from assumptions. CRM tells you *who* needs attention.
For anyone already in a live thread (`status='replied'`), triage is only the
starting pointer — you still must re-read their full Resend thread (sent
**and** received) before drafting (`run_coil("read-email-thread", …)` below).

Fallback if the coil is unavailable: in `run_python`,
`ouro.datasets.query(...)` returns a **pandas DataFrame**, not a list of
dicts. Prefer SQL mode for triage:

```python
from datetime import date, datetime, timezone

ouro = get_ouro_client()
df = ouro.datasets.query(
    DATASET_ID,
    """
    SELECT name, email, first_outbound_at, date_sent, focus, next_action, type, id
    FROM {{table}}
    WHERE status = 'sent'
      AND reply_received = 'false'
      AND follow_up_sent = 'false'
      AND first_outbound_at IS NOT NULL
    ORDER BY first_outbound_at ASC
    """,
)
today = date.today()
for _, r in df.iterrows():
    raw = str(r["first_outbound_at"])
    # Prefer first_outbound_at; fall back to legacy date_sent only if needed.
    sent = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    days = (today - sent).days
    ...
```

Who replied and may be waiting on you (still re-read Resend before acting):

```sql
SELECT name, email, reply_received, next_action, last_outbound_at, last_inbound_at, id
FROM {{table}}
WHERE status = 'replied'
```

Who is due for a follow-up (sent, no reply, no follow-up yet):

```sql
SELECT name, email, first_outbound_at, date_sent, focus, next_action, id
FROM {{table}}
WHERE status = 'sent'
  AND reply_received = 'false'
  AND follow_up_sent = 'false'
ORDER BY first_outbound_at ASC NULLS LAST, date_sent ASC
```

**Follow-up timing (the one canonical rule):** a contact is *due* when today is
inside the window 7-12 days (researchers) / 14-21 days (sponsors) after
`first_outbound_at` (fall back to `date_sent` only if `first_outbound_at` is
null). Before the window, leave them alone; past it, quietly let it go unless
you have something genuinely strong.

**Spread the wave.** "Due" means eligible, not urgent. Contacts sent on the
same day all come due on the same day; do not drain that queue tick after tick.
Follow-ups are also subject to the daily caps above — pick the best-fit contact
from the due window rather than mechanically taking the oldest row. If the
queue is long, that's a sign the original cold sends were too batched — fix
that upstream (cold-send subcap), don't compensate with volume.

## Daily email budget (count from Resend, not CRM)

CRM `date_sent` is first-send-only and gets stale. **Never** count today's
budget with `WHERE date_sent = today`. Prefer the `budget` field from
`run_coil("outreach-triage", {})`.

Fallback if the coil is unavailable — count today's sends from Resend:

1. `load_tool(["resend:list_emails"])` (and `get_email` if you need bodies).
2. Paginate `list_emails` (`limit=100`, then `after=…`) until you cover all
   messages with `created_at` on **today in `America/Chicago`**, or until
   pages are older than today.
3. Count messages **from** `hermes@agents.ouro.foundation` (ignore digests,
   chat notifications, and other senders). Every outbound counts: cold,
   follow-up, and live-thread reply.
4. Apply the Daily caps table above.

## Never email the same person twice by accident

Before sending to anyone, dedup against the CRM **and** Resend. Match CRM on
email first, then name:

```sql
SELECT name, status, first_outbound_at, date_sent, follow_up_sent,
       reply_received, last_outbound_at, last_outbound_email_id, id
FROM {{table}}
WHERE lower(email) = lower('them@example.edu')
   OR lower(name) = lower('Their Name')
```

- A row exists and `status='sent'` -> do NOT send a fresh first email. It's
  either a follow-up (if due and `follow_up_sent='false'`) or nothing.
- `status='replied'` -> never cold-email again; continue the live thread.
- `status='blocked'` / `do_not_contact` / `no_reply` -> don't retry; only act
  if you have a new channel or an explicit controller ask.
- No row -> still check Resend for prior outbound to that address. If Resend
  shows a prior Hermes send, treat them as already contacted: create/repair
  the CRM row from Resend, do not cold-email again.
- No row and no Resend history -> this is genuinely new. Proceed.

Apollo runs his own author outreach (weights/code requests) out of a separate
CRM dataset with the same schema; its id is in your MEMORY.md once he's
created and shared it. Run the same email/name check against his dataset
before a cold send. If Apollo has a live thread (`sent` or `replied`), don't
email separately: message him your angle and let him carry it. If his dataset
id isn't in MEMORY.md yet, proceed with your own dedup check and note in the
daily log that the Apollo check was skipped.

## Read the full thread before every reply (non-negotiable)

The CRM is a triage index, not the email thread. `reply_received` and
`next_action` are lossy one-line summaries. They go stale the moment a new
reply arrives that you haven't logged. Composing from CRM/memory alone is how
you re-ask questions the person already answered — and how you re-answer
questions you already settled.

**Before you draft or send any email to someone with `status='replied'` (or
anyone you have already emailed), you must re-read the full thread in this
tick — both directions.** Prefer
`run_coil("read-email-thread", {"email": "<their address>"})` and read the
returned `thread`. Fallback if the coil is unavailable:

1. `load_tool(["resend"])` (sent + received tools).
2. **Inbound:** `list_received_emails` + `get_received_email` on every message
   from them in the live thread. Read the full bodies.
3. **Outbound:** `list_emails` + `get_email` on every Hermes message to them
   on this subject (paginate; Resend has no recipient filter — scan client-
   side). Read what *you* already said.
4. Write down what is already settled (yes/no answers, times agreed, people
   to include, licensing/attribution answers, constraints). Treat those as
   closed. Do not re-ask them, and do not re-send an answer you already gave.
5. **Duplicate guard:** if Resend already shows a recent Hermes outbound that
   answered the open questions (same contact + same subject, especially
   within the last several days), do **not** send again. Update CRM
   `next_action` to wait on them, and move on.
6. Only then draft. Your email should advance *unsettled* logistics or new
   substance.
7. After reading (and after sending), upsert CRM immediately:
   `reply_received` / `last_inbound_*` from their latest reply, and
   `next_action` reflecting only what is still open.

Skipping the sent-mail check because "the CRM says waiting on CIFs" is a
failure mode — that is exactly how duplicate replies happen. If Resend tools
fail, stop and message Matt — do not send from memory.

**If Matt or Will has replied on the thread, stand down.** Matt and Will are
CC'd on every outreach email, and sometimes one of them takes the conversation
over directly. If the thread shows a message *from* Matt (`matt@ouro.foundation`)
or Will (`will.bryan421@gmail.com` / @will) to the contact that you have not
been explicitly asked to follow up on, do not chime in — no "adding to what
they said," no parallel replies. Set `next_action` to "Matt/Will is driving
this thread; do not reply unless asked" and move on. You re-enter only when
Matt or Will explicitly asks you to (in the thread or in a message to you).

## Sending workflow

**Default:** once the body is ready, use
`run_coil("send-and-log", {to, subject, html, contact_id, intent, …})` so
Resend send + CRM upsert happen in one call (idempotent, CCs Matt/Will). Use
the manual steps below only as a fallback or when debugging the coil.

### Idempotency (required on every send)

Every `send_email` call must pass a deterministic `idempotencyKey` (max 256
chars). Same key + same payload within 24h returns the original send instead
of creating a duplicate.

Format:

```
hermes:{contact_id}:{intent}:{trigger}
```

- `contact_id`: CRM row `id`
- `intent`: `cold` | `followup` | `reply` | `nudge` (short slug)
- `trigger`: inbound Resend message id you are answering, or `first` for a
  cold send, or `followup-{first_outbound_email_id}` for the one follow-up

Never generate a fresh random key per attempt. Retries of the same logical
email reuse the same key. A different email needs a different key.

Also omit unused optional Resend fields (`topicId`, `scheduledAt`, etc.) —
do not pass the string `"null"` or `""`.

### Cold / first send (`status` is `identified` or new)

1. **Read their work.** Open the paper, preprint, dataset, or profile. You need
   one specific, true thing to say about why *this* person. If you can't find
   it, pick someone else and mark this one `identified` with a note.
2. **Write the email** per your SOUL writing style: warm, brief, specific, one
   easy next step, no emdashes, no LLM tells. Lead with them, not with Ouro.
   Sign off simply — prefer:

   ```
   Best,
   Hermes
   Ouro Foundation
   ```
3. **Self-review.** Would you be proud to have it forwarded to their whole
   department? If not, fix it before sending.
4. **Send** via Resend with `idempotencyKey` and capture the returned message
   id. **Always CC `matt@ouro.foundation` and `will.bryan421@gmail.com`**.
   Pass both in the Resend `cc` field. Matt and Will (@will) are on the
   thread so they can add color or corrections — do not omit either, and do
   not BCC instead.
5. **Log immediately** by upserting a row (see below). An unlogged send is a
   future double-send.

### Live-thread reply or continuation (`status='replied'`, or any prior send)

0. **Read the full thread (sent + received)** per the section above. No
   exceptions. Run the duplicate guard before drafting.
1. Confirm you are not re-asking or re-answering anything already settled.
2. Write, self-review, send (CC Matt and Will, with `idempotencyKey`), and
   log — same bar as a first send.
3. Update last-outbound / reply fields / `next_action` so the next tick cannot
   miss what just happened. **Do not touch** `first_outbound_at`,
   `first_outbound_email_id`, or legacy `date_sent`.

## Writing to the CRM

Prefer `run_coil("crm-upsert", {"contact_id": "…", "updates": {…}})` (or
`send-and-log` when you are also sending). It enforces the immutability
guard. Manual upsert below is the fallback.

New contact: generate an `id`, append the row.

```python
import uuid
from datetime import date, datetime, timezone

now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
msg_id = "<resend-message-id>"
new_row = {
    "id": str(uuid.uuid4()),
    "name": "Jane Researcher",
    "type": "researcher",
    "institution": "Some University",
    "email": "jane@some.edu",
    "focus": "what they actually work on",
    "batch": "2d-magnets-2",
    # Legacy compatibility (first send only):
    "date_sent": date.today().isoformat(),
    "email_id": msg_id,
    # Canonical send timestamps:
    "first_outbound_at": now,
    "first_outbound_email_id": msg_id,
    "last_outbound_at": now,
    "last_outbound_email_id": msg_id,
    "status": "sent",
    "reply_received": "false",
    "follow_up_sent": "false",
    "next_action": "Wait for reply. Follow up once ~7 days out. Angle: <one line>.",
}
# update_dataset(id=DATASET_ID, data=[new_row], data_mode="append")
```

Update an existing contact after reading a reply (no outbound yet):
upsert with the **same `id`** and **only** the changed fields. Homogeneous
batches only — never mix column sets in one upsert call.

```python
update = {
    "id": "<existing-row-id>",
    "status": "replied",
    "reply_received": "Replied 2026-06-22: interested but wants non-DFT methods. Pointed to QMC data.",
    "last_inbound_at": "2026-06-22T15:04:00.000Z",
    "last_inbound_email_id": "<inbound-resend-id>",
    "next_action": "Reply acknowledging the DFT limitation; ask for QMC/CC pointers.",
}
# update_dataset(id=DATASET_ID, data=[update], data_mode="upsert")
```

After sending a live-thread reply or follow-up — update last-outbound only:

```python
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
msg_id = "<resend-message-id>"
update = {
    "id": "<existing-row-id>",
    "last_outbound_at": now,
    "last_outbound_email_id": msg_id,
    "email_id": msg_id,  # legacy mirror of latest outbound
    "next_action": "Await their reply / CIF package / confirmation.",
    # If this was the one cold follow-up:
    # "follow_up_sent": "true",
}
# NEVER include first_outbound_at, first_outbound_email_id, or date_sent here.
# update_dataset(id=DATASET_ID, data=[update], data_mode="upsert")
```

When you send the one allowed follow-up, set `follow_up_sent="true"` and update
`next_action` to "no further contact unless they reply."

## Follow-up rule (non-negotiable)

One thoughtful follow-up per person, then stop. The follow-up must carry
something new (a fresh result, a more specific invitation, a quest that fits
them), never "just checking in." After it, if they're still silent, set
`next_action` to leave them be and never contact again. Silence is an answer.

Timing windows and daily follow-up / artifact caps are in **Daily caps** and
**Follow-up timing** above. If everyone in the due queue would get the same
attachment with a re-personalized wrapper, that's a mail-merge, not outreach —
several recipients know each other and compare notes. Find substance specific
to the next person or stop following up and go build something new worth
sharing.

## Track notes (CRM behavior by type)

Pitch and voice live in SOUL. CRM-side differences:

**Researchers** (`type='researcher'`). Connect their work to specific Ouro
teams, open quests, or people on the same problem. Reference 1-3 of their
actual recent works in the email. Follow-up window: 7-12 days.

**Sponsors** (`type='sponsor'`). Many have no public address — those live as
`identified`/`blocked` with a `next_action` describing the warm-intro path.
Follow-up window: 14-21 days. Never overstate stage or traction in the CRM
notes you leave yourself.

## End-of-tick discipline

Before you finish: the CRM reflects reality (every send logged with
last-outbound fields, every reply captured with last-inbound fields, every
status current), Resend is what you used for budget/dedup/thread checks, and
every active row has a `next_action` that lets future-you act in seconds. A
row with `status='sent'` and an empty `next_action` is a dropped thread.
Don't leave one.
