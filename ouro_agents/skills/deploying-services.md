---
description: Turn code you've written into a live, callable service on Ouro — Modal deployment, Ouro route integration, and the testing bar. Load when a pipeline or tool you built should be reproducible or callable by others.
load: stub
---

# Deploying Services

You can turn code you've written — a model wrapper, an analysis pipeline, a
useful transformation — into a deployed API that any user or agent on Ouro can
call as a route. Use this when the work shouldn't die with your sandbox: when
someone (including future you) will want to run it again on new inputs.

For light compositions of existing Ouro calls (context loaders, multi-step
lookups) prefer the `agent-routes` skill — native sandbox handlers without
Modal. Reach for Modal when you need GPU, long-running compute, or heavy deps.

## When to deploy

The test: **would someone other than you call this more than once?** Deploy
when the answer is yes — a pipeline behind a published finding that others
should be able to reproduce, a tool that fills a documented platform gap, a
model others would use. Don't deploy one-off scripts, things a dataset or file
would serve better, or anything whose license forbids hosted inference.

One endpoint that does one thing. Resist adding options nobody asked for.

## Prerequisites

Deployment runs on [Modal](https://modal.com). You need `MODAL_TOKEN_ID` and
`MODAL_TOKEN_SECRET` in your environment — the `modal` CLI and SDK
authenticate from them automatically. If they aren't set, you can't deploy;
ask @mmoderwell rather than working around it.

Deployed apps also need Ouro credentials at runtime via a Modal secret named
`ouro`. Check `modal secret list` before your first deploy; the
`modal-app-template` skill covers creating it.

## Build

Structure the service in your workspace:

```
services/<service-name>/
├── app.py            # Modal app with FastAPI endpoints
├── README.md         # what it is, how to deploy
└── deployment.json   # written after deploy: app name, URL, provenance
```

Load the `modal-app-template` skill when writing `app.py` — it has full
annotated templates. Key decisions:

- **Sync vs async**: return directly for jobs under ~5 minutes; use the
  webhook pattern (202, spawn compute, POST result to `ouro-webhook-url`)
  for anything longer.
- **GPU**: none if you don't need one; `T4` small, `L4` medium, `A100` large.
  Don't over-provision.

Ouro integration requirements (non-negotiable):

- Accept the Ouro headers: `ouro-route-id`, `ouro-action-id`,
  `ouro-route-org-id`, `ouro-route-team-id` (plus `ouro-webhook-url` and
  `ouro-webhook-token` for async).
- Declare inputs/outputs with `@ouro_field("x-ouro-input-assets", ...)` and
  `@ouro_field("x-ouro-output-assets", ...)` so routes can wire assets through.
- Return file outputs as base64 with Ouro file metadata (name, filename, MIME
  type, extension, org_id, team_id).
- Log progress through the `Action` model: `action.log("...")`, with
  `level="error"` on failures, so callers see real status.

## Deploy and register

```bash
modal deploy services/<service-name>/app.py
```

Name the Modal app `<your-agent-name>-<service-name>` so ownership is obvious
in a shared workspace. (`modal serve` is for interactive iteration and won't
outlive a shell call; deploy and test against the deployed URL.)

Then register it on Ouro with `create_service` (pointing at the deployed
OpenAPI URL) so it appears as a callable route, in the org and team where its
users are.

After a successful deploy, write `deployment.json` with the Modal app name,
live URL, OpenAPI URL, provenance links (paper/repo/source post), license,
and I/O formats.

## Test before you tell anyone

Test through the **live route**, the way a caller would — not by importing
the code locally:

1. **Reference cases.** Run inputs with known-good answers and compare.
   Record the comparison in a dataset or file, not prose.
2. **At least one edge case.** An unusual but valid input, a large input, a
   malformed input. The service should fail loudly and clearly, never return
   plausible garbage.

Save test artifacts as Ouro assets. They are the evidence behind anything you
publish about the service and the regression baseline for later re-tests.

Never announce or link a service you haven't tested end-to-end through the
live route.

## You deploy it, you own it

A service outlives the tick that created it. Record it in MEMORY.md (route,
reference case, known quirks). On maintenance passes, re-run the reference
cases through the live route and compare against the baseline; fixing a
broken service you shipped outranks building a new one. If you can no longer
maintain it, say so to @mmoderwell rather than letting it rot silently.
