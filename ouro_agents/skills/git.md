---
description: Own your repository with git and GitHub CLI — inspect changes, branch, test, commit, push, and open reviewable pull requests
load: always
---

# Git and GitHub

Your workspace is your repository. You may inspect and improve it with
`run_shell`; `git` and the authenticated `gh` CLI are available in the Docker
sandbox. Treat its history as durable evidence of what you learned and changed.

## Change workflow

1. Start clean: `git status --short --branch`. Read `git diff` and do not discard
   changes you did not make.
2. Update from the default branch, then create a focused branch:
   `git switch -c <type>/<short-purpose>`. Never work directly on `main`.
3. Make the smallest coherent change. Keep scratch work under `scratch/`, which
   is ignored; move only durable code, identity, skills, or curated memory into
   tracked paths.
4. Run the relevant tests and inspect `git diff --check` plus
   `git diff --stat`. Never commit a change you have not reviewed.
5. Stage named files, not everything: `git add path/to/file ...`. Check
   `git diff --cached` before committing.
6. Commit with a short imperative message (`fix:`, `feat:`, `docs:`, `chore:`).
7. Push the branch and open a PR:
   `git push -u origin HEAD` then
   `gh pr create --fill --body "<what changed, why, and how it was tested>"`.
8. Report the PR URL. Do not merge your own PR unless a controller explicitly
   authorized that repository and change class.

## Boundaries

- Never commit `.env`, credentials, API keys, tokens, private keys, opaque
  memory databases, conversation transcripts, or unreviewed generated output.
- Never use `git push --force`, rewrite shared history, bypass branch protection,
  delete branches you did not create, or change repository visibility/access.
- Do not overwrite or clean another person's uncommitted work. If the tree is
  unexpectedly dirty, isolate your files or ask a controller.
- Your GitHub token defines your authority. A permission error is a boundary,
  not a problem to work around.
- Changes to the public `ouro-agents` runtime always go through a PR and human
  review. Pin released PyPI versions in your own project; do not depend on a
  local runtime checkout.
