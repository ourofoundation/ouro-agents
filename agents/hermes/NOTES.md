# Notes

## Workspace reorganization (2026-07-15)

The workspace root was cleaned up. Your files now live under a fixed layout —
follow it for all new files (see the WORKSPACE FILE ORGANIZATION prompt section):

- `projects/<slug>/` — per-effort artifacts. Existing: `novomag/` (screening
  protocols, MAE dataset, pipeline scripts, cifs), `scigen-cycles/` (all
  cycleN posts/results/cifs), `outreach/` (CRM exports, sponsor files,
  oliynyk materials), `audits/`, `analyses/` (one-off analysis posts),
  `research/`, plus topic dirs (altermagnets, hydrides, nickelates, kitaev,
  spinel, perovskite, mofs, nvpf, dirhenate, nb3cl8).
- `drafts/` — email and follow-up drafts (and `drafts/posts/`).
- `cifs/` — structure files: `library/` (reference minerals), `misc/` (loose
  structures), plus existing subdirs.
- `scratch/` — disposable; `scratch/legacy/` holds the old DAILY/daily_logs dirs.

Do not write new files at the workspace root. Overwrite working files in place
instead of creating `_v2`/`_fixed` copies.
