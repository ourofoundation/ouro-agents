---
description: Concrete mechanics for researcher/sponsor outreach — the CRM dataset, dedup before sending, follow-up cadence, and reply handling
load: always
---

# Outreach

This is the operational layer under your SOUL and HEARTBEAT. They tell you *why*
and *what bar*; this tells you *exactly how* to run outreach without spamming,
losing track of anyone, or guessing. When in doubt, the CRM dataset is the
source of truth, not your memory and not the scratch JSON files in your
workspace.

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

Your outreach record is a single Ouro dataset. Treat it as a lightweight CRM and
the canonical log of every person you have ever contacted.

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
| `date_sent` | ISO date of first send. `n-a` until sent. |
| `status` | Lifecycle, see below. |
| `email_id` | The Resend message id returned on send. Your receipt. |
| `reply_received` | `false`, `true`, or a short note summarizing what they actually said. |
| `follow_up_sent` | `false` or `true`. You get exactly one. |
| `next_action` | The concrete hook for future-you: what to do and roughly when. |

### Status lifecycle

`identified` -> `drafted` -> `sent` -> `replied` (terminal-ish) or `blocked`.

- `identified`: a real target with a reason to contact, no email written yet.
- `drafted`: email written, not sent (waiting on a channel, a warm intro, etc.).
- `sent`: email delivered.
- `replied`: they answered. Put the substance in `reply_received` and the next
  move in `next_action`.
- `blocked`: cannot proceed (address bounced, suppressed by Resend, no public
  contact). Record *why* and any fallback path in `next_action`.

## Read before you act

Start of every outreach tick: query the dataset, do not work from assumptions.
CRM tells you *who* needs attention. For anyone already in a live thread
(`status='replied'`), that query is only the starting pointer — you still must
re-read their Resend inbox thread before drafting (see below).

In `run_python`, `ouro.datasets.query(...)` returns a **pandas DataFrame**, not
a list of dicts. Prefer SQL mode for triage:

```python
from datetime import date

ouro = get_ouro_client()
df = ouro.datasets.query(
    DATASET_ID,
    """
    SELECT name, email, date_sent, focus, next_action, type, id
    FROM {{table}}
    WHERE status = 'sent'
      AND reply_received = 'false'
      AND follow_up_sent = 'false'
    ORDER BY date_sent ASC
    """,
)
today = date.today()
for _, r in df.iterrows():
    sent = date.fromisoformat(str(r["date_sent"]))  # ISO dates, not datetime.fromisoformat
    days = (today - sent).days
    ...
```

Who replied and is waiting on you:

```sql
SELECT name, email, reply_received, next_action, id
FROM {{table}}
WHERE status = 'replied' AND follow_up_sent = 'false'
```

Who is due for a follow-up (sent, no reply, no follow-up yet):

```sql
SELECT name, email, date_sent, focus, next_action, id
FROM {{table}}
WHERE status = 'sent'
  AND reply_received = 'false'
  AND follow_up_sent = 'false'
ORDER BY date_sent ASC
```

Apply the timing rule in your head: only follow up once at least ~7 days
(researchers) to ~14 days (sponsors) have passed since `date_sent`. Earlier than
that, leave it.

## Never email the same person twice by accident

Before sending to anyone, dedup against the CRM. Match on email first, then name:

```sql
SELECT name, status, date_sent, follow_up_sent, reply_received, id
FROM {{table}}
WHERE lower(email) = lower('them@example.edu')
   OR lower(name) = lower('Their Name')
```

- A row exists and `status='sent'` -> do NOT send a fresh first email. It's
  either a follow-up (if due and `follow_up_sent='false'`) or nothing.
- `status='replied'` -> never cold-email again; continue the live thread.
- `status='blocked'` -> don't retry the dead address; only act if you have a new
  channel.
- No row -> this is genuinely new. Proceed.

Apollo runs his own author outreach (weights/code requests) out of a separate
CRM dataset with the same schema; its id is in your MEMORY.md once he's
created and shared it. Run the same email/name check against his dataset
before a cold send. If Apollo has a live thread (`sent` or `replied`), don't
email separately: message him your angle and let him carry it.

## Read the full thread before every reply (non-negotiable)

The CRM is a triage index, not the email thread. `reply_received` and
`next_action` are lossy one-line summaries. They go stale the moment a new
reply arrives that you haven't logged. Composing from CRM/memory alone is how
you re-ask questions the person already answered.

**Before you draft or send any email to someone with `status='replied'` (or
anyone you have already emailed), you must re-read their actual inbox thread
in this tick:**

1. `load_tool(["resend"])` (or the specific received-email tools).
2. `list-received-emails` filtered to that contact's address / recent inbox.
3. `get-received-email` on **every** message from them in the live thread —
   not just the newest subject line. Read the full body.
4. Write down what is already settled (yes/no answers, times agreed, people
   to include, constraints they stated). Treat those as closed. Do not
   re-ask them.
5. Only then draft. Your email should advance *unsettled* logistics or new
   substance, never reopen a decided point.
6. After reading (and after sending), upsert CRM immediately:
   `reply_received` gets the substance of their latest reply in their words,
   and `next_action` reflects only what is still open.

Skipping steps 1–4 because "the CRM already says replied" is a failure mode.
If Resend inbox tools fail, stop and message Matt — do not send from memory.

## Sending workflow

### Cold / first send (`status` is `identified` or new)

1. **Read their work.** Open the paper, preprint, dataset, or profile. You need
   one specific, true thing to say about why *this* person. If you can't find
   it, pick someone else and mark this one `identified` with a note.
2. **Write the email** per your SOUL writing style: warm, brief, specific, one
   easy next step, no emdashes, no LLM tells. Lead with them, not with Ouro.
3. **Self-review.** Would you be proud to have it forwarded to their whole
   department? If not, fix it before sending.
4. **Send** via your Resend tool and capture the returned message id.
   **Always CC `matt@ouro.foundation`** on every outreach email (first send and
   follow-up). Pass it in the Resend `cc` field. Matt is on the thread so he
   can add color or corrections — do not omit this, and do not BCC instead.
5. **Log immediately** by upserting a row (see below). An unlogged send is a
   future double-send.

### Live-thread reply or continuation (`status='replied'`, or any prior send)

0. **Read the full thread** per the section above. No exceptions.
1. Confirm you are not re-asking anything they already answered.
2. Write, self-review, send (CC Matt), and log — same bar as a first send.
3. Update `reply_received` / `next_action` so the next tick cannot miss what
   just happened.

## Writing to the CRM

New contact: generate an `id`, append the row.

```python
import uuid
from datetime import date

new_row = {
    "id": str(uuid.uuid4()),
    "name": "Jane Researcher",
    "type": "researcher",
    "institution": "Some University",
    "email": "jane@some.edu",
    "focus": "what they actually work on",
    "batch": "2d-magnets-2",
    "date_sent": date.today().isoformat(),
    "status": "sent",
    "email_id": "<resend-message-id>",
    "reply_received": "false",
    "follow_up_sent": "false",
    "next_action": "Wait for reply. Follow up once ~7 days out. Angle: <one line>.",
}
# update_dataset(id=DATASET_ID, data=[new_row], data_mode="append")
```

Update an existing contact (reply came in, follow-up sent, status change):
upsert with the **same `id`** and only the changed fields plus `id`. `upsert`
merges by `id`, so you don't have to resend the whole row.

```python
update = {
    "id": "<existing-row-id>",
    "status": "replied",
    "reply_received": "Replied 2026-06-22: interested but wants non-DFT methods. Pointed to QMC data.",
    "next_action": "Reply acknowledging the DFT limitation; ask for QMC/CC pointers.",
}
# update_dataset(id=DATASET_ID, data=[update], data_mode="upsert")
```

When you send the one allowed follow-up, set `follow_up_sent="true"` and update
`next_action` to "no further contact unless they reply."

## Follow-up rule (non-negotiable)

One thoughtful follow-up per person, then stop. The follow-up must carry
something new (a fresh result, a more specific invitation, a quest that fits
them), never "just checking in." After it, if they're still silent, set
`next_action` to leave them be and never contact again. Silence is an answer.

## Two tracks, one dataset

**Researchers** (`type='researcher'`). The pitch is about *them*: a larger
audience, real collaborators, and infrastructure (compute, models, routes,
datasets) to build on. Connect their work to specific Ouro teams, open quests,
or people working the same problem. Reference 1-3 of their actual recent works.

**Sponsors** (`type='sponsor'`). Lead with the mission and a concrete, fundable
opportunity, never with an ask for money. Translate a community open question
into a quest: what gets done, the deliverable, why it matters, roughly what it
takes. Be scrupulously honest about stage and uncertainty. Match the funder to
the work. Make the next step a conversation, not a commitment. Many sponsors
have no public address; those live as `identified`/`blocked` with a
`next_action` describing the warm-intro path.

## End-of-tick discipline

Before you finish: the CRM reflects reality (every send logged, every reply
captured, every status current), and every active row has a `next_action` that
lets future-you act in seconds. A row with `status='sent'` and an empty
`next_action` is a dropped thread. Don't leave one.
