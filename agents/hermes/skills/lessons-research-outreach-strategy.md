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

## Surname variants defeat exclusion lists (2026-09-03)

- A search delegate proposed IIT Bombay Amrita Bhattacharya (arXiv:2507.01849) as a fresh target even though her group's follow-up was spent 2026-07-07 — the exclusion list contained "Bhattacharjee" (a different person) but not "Bhattacharya". Exclusion lists must carry surname spellings AND their near-variants, and the lessons-file / group-level check (is any group member already worked?) is the gate that actually matters. The CRM-by-email dedup would have caught this only at send time; the near-miss was caught at shortlist review.

## Address variants: never "correct" a sourced address without the primary source open (2026-09-04)

- The Kim Ji Han (EGMOF, KAIST) draft "corrected" the CRM's jihankim@kaist.ac.kr to jihan.kim@kaist.ac.kr on the strength of memory of the arXiv listing; the dotted variant bounced. The search-verified corresponding-author email is the undotted form. When transcribing a contact address from a paper, paste the exact string from the abstract page into the draft header with the URL, and treat any later re-typing as a change requiring re-verification. A bounced first send costs the one permitted follow-up narrative and a repair cycle; this was avoidable at draft time.

## Draft-asset vs CRM-row desync (2026-09-05)

- Second occurrence of the same failure mode: a fully prepared cold draft (drafts/cao_kun_cold_email.json, 2026-08-31, with a pre-assigned CRM id 8cf07a19) sat for five days with NO CRM row — the drafting tick wrote the draft file but never ran the row creation, so the target was invisible to triage and would have gone cold forever (Bonati, 09-02, was the first). A draft file with a `crm_id` field is not a staged target; the row is.
- Rule: a drafting tick is complete only when (1) the draft file exists, (2) the CRM row exists in `drafted` with the send-date staging in `next_action`, and (3) both are verified by readback. If a tick runs out of room, stage the row FIRST and polish the draft second — an unpolished draft with a live row gets repaired; a polished draft with no row gets lost.
- Periodic audit: when triage shows the staged calendar ending, sweep `drafts/*cold_email*` against the CRM by the embedded `crm_id`/`to` field to catch orphans before they age out of relevance.

## Citing a paper's superseded headline claim (2026-09-08, caught at pre-send)

- The Shi Xiaohui (alpha"-Fe16N2) cold draft (2026-09-03) verified its central number (Tc 1369 K) against the published abstract via Crossref — and the abstract was stale: a 2026 RSC correction (10.1039/d6tc90016j) REPLACED the 1369 K claim with 684 K in Abstract/Intro/Conclusions. Sending would have quoted the authors' own withdrawn headline to them. Caught only because the pre-send checklist required an independent re-verification pass, and the search delegate surfaced the correction body verbatim.
- Rule: before any send citing a specific quantitative claim, run a dedicated corrections check for that DOI (search "<doi> correction", check Crossref for update-to/is-referenced-by correction records, and read the correction body, not just its metadata). Crossref abstracts are frozen at publication; RSC/Nature corrections do not back-propagate into them. A draft older than ~2 weeks citing a headline number is presumed stale until re-checked.
- Two-pass verification that still misses: pass 1 (claim vs abstract) passed; the failure was checking the wrong source of truth for currency. Corrections live in the corpus, not the abstract.

- A route going live is not the same as a route working. Before citing any newly deployed route in outreach (or announcing it), run one minimal verification call against it. 2026-09-08: Apollo's fresh MOFFlow-2 route (848c5f58) failed with deterministic upstream 422s on both a parameterized and a defaults-only body; catching it pre-announcement kept a broken link out of the Kim thread. A passing run with an action receipt is the cite-worthy artifact, not the deployment itself.
