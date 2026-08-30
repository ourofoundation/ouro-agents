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
