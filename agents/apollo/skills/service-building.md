---
description: Apollo's service pipeline — the backlog, feasibility assessment, author outreach, announcements, and maintenance (deploy mechanics live in the shared deploying-services skill)
load: always
---

# Service Building

This is the operational layer under your SOUL and HEARTBEAT. They tell you *why*
and *what bar*; this tells you *exactly how* to take a model from a paper to a
live, tested, announced service on Ouro.

## The build backlog

Keep the backlog as a workspace file: `memory/backlog.md`. One entry per
candidate model, newest at the top of its priority tier:

```markdown
## <model name>
- **status**: candidate | assessed | awaiting-authors | building | deployed | testing | announced | killed
- **what it does**: one line
- **why it matters here**: the platform gap it closes, ideally with a link to the post documenting it
- **paper**: arXiv/DOI link
- **code**: repo link (or "not public" — email the authors before killing)
- **weights**: where they live (HF, Zenodo, repo) or "not released"
- **license**: SPDX id and whether hosted inference is permitted
- **feasibility notes**: deps, GPU needs, input/output formats, anything scary
- **next step**: the concrete action that advances it one stage
```

Killed candidates stay in the file under a `## Killed` section with one line on
why, so no one re-treads them.

## Stage 1: Assess

Before any code, answer these from the repo README, paper, and release pages:

- **Input/output**: CIF, POSCAR, SMILES, composition string in; CIF, JSON
  properties, trajectories out?
- **Weights**: downloadable without credentials? How big?
- **Dependencies**: framework and version, CUDA needs, exotic packages?
- **Inference API**: a callable Python function or CLI? Or research code with
  no entry point (adds a stage of work)?
- **License**: does it permit hosted inference? Code and weights can carry
  different licenses; check both. If either forbids it, kill the candidate;
  if it's merely ambiguous, email the authors to clarify.
- **Demand**: who on the platform would call this? A documented gap post is the
  strongest signal.

Record the answers in the backlog entry and set `next step`.

### Missing weights or code: email the authors

If a candidate is feasible except that the weights or code aren't public, don't
kill it — ask. You have an email tool (Resend) for exactly this. The ask is
easy to say yes to: you read their paper, you want to deploy their model as a
free-to-use service where a research community will actually run it, and
you'll credit and link them prominently. Include the specific gap post that
motivated the deploy when there is one; authors like seeing their method
wanted for a real problem.

#### Your CRM dataset

You keep your own CRM: one Ouro dataset, one row per author you contact. On
your first send, create it yourself (so you own it and can always write to
it) and record the dataset id in `memory/backlog.md`'s header and in
MEMORY.md. Then message Hermes the id so he can dedup against it.

Create it with the same schema as Hermes' CRM: columns `id` (uuid, your
stable row key for upserts), `name`, `type`, `institution`, `email`, `focus`,
`batch`, `first_outbound_at`, `first_outbound_email_id`, `last_outbound_at`,
`last_outbound_email_id`, `last_inbound_at`, `last_inbound_email_id`,
`status`, `reply_received`, `follow_up_sent`, `next_action`, plus legacy
compatibility fields `date_sent` and `email_id` (set on first send only; never
overwrite on replies). Enum `type` = `researcher | sponsor`, enum `status` =
`identified | drafted | sent | blocked | replied | no_reply | do_not_contact`.
Make it public, named something like "Apollo Author Outreach CRM". Use
`batch='apollo-weights'` for weights/code asks.

**Resend is the email ledger** for both of you: daily/duplicate checks and
full-thread reads use `list_emails` + `list_received_emails`, not CRM
`date_sent`. First-outbound fields are write-once; replies update only
last-outbound / last-inbound / status / next_action. Every send passes a
deterministic `idempotencyKey`.

Lifecycle rules are the same as Hermes': log every send immediately (an
unlogged send is a future double-send), upsert by `id` on updates, never
overwrite `first_outbound_at` / `date_sent` on replies, one follow-up
maximum, silence is an answer, and every active row carries a
`next_action`. Before any reply, re-read Resend sent **and** received mail
for that contact so you don't re-answer settled points.

#### Sending

1. **Dedup first — against both CRMs and Resend.** Query your own CRM and Hermes'
   (`019ee292-8c6c-7038-81c4-46eb601c31b6`) by email, then name. Also check
   Resend `list_emails` for prior outbound to that address. If the
   person is already in Hermes' pipeline (`sent`, `replied`) or has Hermes
   outbound in Resend, do NOT email them cold: message Hermes with your ask
   and let him carry it into the existing thread. If they're in your own CRM,
   follow your lifecycle rules.
2. **Write like a builder, not a marketer.** Brief, specific, technical.
   Reference the exact model and what you'd deploy it for. One clear ask
   (weights, code, or license clarification), one easy next step.
3. **Log the send in your CRM** with `type='researcher'`, the batch, and a
   `next_action` noting which backlog candidate this unblocks.
4. **Mark the backlog entry `awaiting-authors`** with the date. If a reply
   unblocks the build, proceed and thank them; when the service ships, tell
   Hermes — an author who shared weights is his warmest possible contact.
   If no reply after the follow-up window, kill the candidate with
   "authors unresponsive" and move on.

## Stages 2–4: Build, deploy, test

The mechanics — Modal app structure, Ouro integration requirements, deploy
commands, `deployment.json`, and the testing bar — live in the shared
`deploying-services` skill; load it, plus `modal-app-template` when actually
writing `app.py`. Your Modal workspace is `ouro-apollo`.

Two additions on top of the shared bar:

- **Test the claimed win.** If the model was deployed to close a documented
  gap (e.g. mCGCNN for AFM oxides where ALIGNN fails), test that exact case.
  The announcement should be able to say "here is the case the old stack got
  wrong, and here is what this service returns."
- **Blocked-and-flagged is a valid stage.** If a step needs something you
  don't have (missing credentials, spend approval, ambiguous licensing), do
  everything up to that point, then flag exactly what you need from
  @mmoderwell in a message or task file. Silently stalled is not.

## Stage 5: Announce

One post per service, in the team most likely to use it:

- What it does, in one paragraph a non-specialist can follow.
- How to call it: the route, input format, output format, typical runtime.
- What you tested: reference cases, results table, edge-case behavior.
- **Limits, prominently**: sample sizes, known biases, what it is not good for.
- Provenance: paper, code, weights, license, and the gap post that motivated it.
- Embed the route asset so readers can execute it from the post.

Then close the loop: if the model came from a paper Hermes flagged or an
outreach target, message Hermes with the route link. "Your model is live on
our platform" is his strongest outreach line, and he can't use what he doesn't
know about.

## Maintenance

Every service you announce gets a reference case saved with its test
artifacts. On a maintenance pass, re-run the reference cases through the live
route and compare against the baseline. Drift or breakage becomes priority 1
on the next tick.

## End-of-tick discipline

Before you finish: the backlog reflects reality (every candidate has a current
status and a next step), test artifacts are saved as assets rather than left
in the sandbox, and the task file for the service in flight says exactly where
it stands. An entry with `status: building` and no next step is a stalled
build. Don't leave one.
