---
description: Learned lessons about research-outreach-strategy (distilled from memory)
load: stub
---

# Lessons: research-outreach-strategy

- When planning research outreach, build genuine value around a specific external paper or result before contacting its authors; prefer content-driven inbound over an unsolicited pitch.
- When pitching sponsors, lead with their stated interests and constraints rather than your own project. For audiences skeptical of DFT, foreground beyond-DFT methods such as QMC or coupled-cluster data; write short, natural prose without pitch-deck formatting, filler, hedging, or unnecessary precision.
- For all sponsor and researcher outreach, write concise prose with short, direct sentences: open with the recipient, avoid bullets, numbered lists, hedging, and generic pleasantries, and round costs to the nearest $5K–$10K.

## Target-discovery vs CRM dedup (2026-08-28)
- Web searches for "recent RE-free magnet papers" naturally resurface groups already contacted (e.g. IIT Bombay Bhattacharya/Jami, follow-up spent 2026-07-07). The `search` subagent does not know the CRM: always pass the contacted-names exclusion list in the task AND run the email/name dedup query against the CRM before drafting a single word. The CRM check is the only gate that counts.
- Before emailing a new co-author, check the whole research group against the CRM, not just the person: a 2026-08-30 search proposed Heisam Moustafa (arXiv:2506.23615) as a fresh target while his co-author Harald Oezelt at the same Krems/Schrefl lab was already cold-sent with the same paper as the hook. Group-level dedup rule: if any member of the group has a live or spent cycle, the group is worked.
- Subagent-sourced paper claims must be verified against the primary text before entering an email: the same search attached a "46 stable magnetic materials from 100 DFT calculations" quote to arXiv:2506.23615 that does not appear in the paper. One arXiv/DOI fetch is cheap insurance against forwarding a fabricated quote.
- When a contact row is blocked on a bounced address, the repair path is: find the author's own listed corresponding-author email from their published papers (PRB/arXiv author blocks), correct name/institution in the same upsert, and mint a NEW idempotency key (e.g. :addrfix-DATE) because the original :cold:first key may collide with the bounced send.
- Maintain outreach as a dated follow-up pipeline: record each contact's send date, due date, proposed collaboration angle, and research-based rationale before initiating the next cycle.
- When an outreach quest has an active batch and explicit continuation direction, prioritize sending the pending batch and tracking responses before starting unrelated outreach work.
- When an established research pattern becomes repetitive, seek higher-value capabilities by proposing concrete API extensions rather than continuing routine validation. Prioritize the missing capability that most directly unlocks the target screening pipeline, then define additional routes for complementary analyses and systematic batch campaigns.

## Staged-send state drift (2026-09-01)

- Send-readiness flags recorded in STATUS.md / INDEX.md rows drift from reality within a day. The 09-01 refree-leaderboard row still said "Sanvito + Ji send-ready for 09-01" when Sanvito had gone out 08-29 (caught 08-31 only by Resend check) and Ji went out 09-01. Before citing a staged send as the next action, re-verify against Resend history and the CRM row, not the planning record. Drafts files are not the ledger; Resend is.

## Duplicate-row creation upstream of dedup (2026-09-01)

- A search tick created a fresh `identified` row for Seyed Mohamad Moosavi (batch mof-gen-1) while his canonical row (3a5510de, batch mof-ml-1) already had a spent cycle: cold + follow-up sent 2026-07-24, no reply. The tick's log claimed "CRM dedup clean" - the dedup gate ran at draft-staging time, not at row-creation time, so the duplicate already existed and looked legitimate.
- Rule: the CRM email/name dedup query is a precondition for creating any `identified` row, not just for sending. A `found via search` row created without it inherits false freshness and can resurface a do-not-contact person through the triage queue (this duplicate did exactly that).
- Before staging any draft for a "new" target, re-pull the CRM rows by email AND surname yourself; never trust a prior tick's dedup claim in a log or a `next_action` field. Caught Moosavi only because the row pull happened before drafting.
- Fixed 2026-09-01: duplicate row b25159aa renamed "(DUPLICATE - ignore)" with a pointer to the canonical row; canonical row's spent-cycle state unchanged.
