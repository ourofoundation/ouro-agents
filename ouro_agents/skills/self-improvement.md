---
description: Close the Ouro service feedback loop — inspect real route evidence, improve owned code, submit it for review, deploy, and verify
load: stub
---

# Service Self-Improvement

Use this loop when a service or coil you own fails, produces weak results, or
shows a repeated source of friction. Improve from concrete Ouro evidence, not
from speculative cleanup.

## Observe

1. Resolve the exact service or route on Ouro.
2. Inspect recent executions with `list_route_actions`. For a relevant action,
   use `get_action` and `get_action_logs`; inspect its input/output assets when
   needed.
3. Read user comments and connected artifacts. State the smallest reproducible
   failure and distinguish code defects from bad inputs, unavailable
   dependencies, permissions, and upstream failures.

## Change

1. Locate the owning code in this repository and read its tests and deployment
   notes.
2. Reproduce locally with the smallest safe case. Add a regression test before
   or with the fix when practical.
3. Make one focused correction. For a coil, use `run_coil` against a reference
   case and an edge case. For a deployed service, follow its own test and deploy
   instructions.
4. Follow the always-loaded `git` skill: branch, test, commit, push, and open a
   PR. Link the Ouro action or artifact that motivated the change in the PR.

## Ship and verify

After review and merge:

1. Deploy the merged revision. For a coil, call `publish_route`; for an external
   service, deploy it first and sync the Ouro service/route metadata if its
   contract changed.
2. Execute the live Ouro route with the original reproducer and one known-good
   control. Do not call a local handler and label that production verification.
3. Inspect the resulting action and logs. Link the verified action in the
   originating thread or work record.
4. Record only durable learning: update the owning skill when future behavior
   should change, and update `MEMORY.md` when the fact is specific to your
   ongoing work. Remove temporary repro files.

If deployment credentials, repository permissions, or review are missing,
stop at the boundary and hand off the branch/PR plus exact verification steps.
