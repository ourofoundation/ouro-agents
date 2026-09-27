---
description: Local-only git — commit reviewable changes on focused branches; pushing and pull requests are not available in this deployment
load: always
requires: git-local
---

# Git (local only)

Your workspace is a git repository and `git` works in the Docker sandbox, but
this deployment cannot push or open pull requests. Do not try `git push`,
`gh`, or editing remotes; they will fail, and retrying wastes the tick.

## What to commit

Code, coils, skills, `SOUL.md`, `HEARTBEAT.md`, and other identity or
procedure changes, so a controller can review and publish them. Logs, team
memory files, and working notes that change every tick do not need commits.

## Change workflow

1. Start with `git status --short --branch`. Do not discard changes you did not
   make.
2. Branch from local `main`, not from whatever branch you are on:
   `git switch -c <type>/<short-purpose> main`. If uncommitted files block the
   switch, commit on the current branch instead and say so in the message.
3. Make the smallest coherent change, then check `git diff --check` and
   `git diff --stat`.
4. Stage named files only (`git add path/to/file ...`), review
   `git diff --cached`, and commit with a short imperative message (`fix:`,
   `feat:`, `docs:`, `chore:`).
5. Do not ask a controller per commit. When a change needs human review before
   it matters (code, coils, identity), ask once with `ask_controller`, naming
   the branches, and check Standing Controller Decisions first so you never
   re-ask. Without `ask_controller`, name the branch in your run summary.

## Boundaries

- Never commit `.env`, credentials, tokens, private keys, memory databases, or
  conversation transcripts.
- Never rewrite history, delete branches you did not create, or clean another
  person's uncommitted work.
