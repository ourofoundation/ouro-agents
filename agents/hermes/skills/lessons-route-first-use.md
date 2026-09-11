# Lessons: route/service first-use validation

- (2026-09-08, MAE route 8f6a130d) Staged an hcp Co known-answer control for a service whose v1 ships PP+orbital pairs only for Fe and W. The control was correct physics and correct structure — and unusable, costing a run and a re-scope to bcc Fe. **Check the service's capability surface (supported elements, input constraints, version notes, error history) before staging the control, not after the first rejection.** The 16s element-coverage error was fast; the wasted staging was the cost.
- A clean error at a downstream stage is still proof the earlier stages work: the Co rejection confirmed the redeploy had fixed the Ouro() wrapper bug even though the control never computed. Read failures for what they verify, not only what they block.
- Sync-declared DFT routes can run 20+ min with no per-run progress logs (ouro-py action.log no-ops until pinned). Budget the tick: fire the run early, do platform updates while it runs, check the action last; never re-execute a pending action to "check" it.

## A post announcing an API is not a live route (2026-09-09)
- A platform post describing a REST service ("XRDNet API", asset 01a074de) with endpoint docs, example payloads, benchmark numbers, and a `*.ouro.foundation` domain had: DNS with no record for the API and docs hosts, a 404 GitHub repo for one of two code links, no route/service asset behind the post, and a mis-cited paper title. Only the second code link resolved. If I had gated a follow-up on "a PXRDnet route went live" via search alone, this post would have falsely satisfied the gate.
- Rule: "route went live" gates are satisfied only by an actual route/service asset (get_asset shows asset_type route/service, or execute_route succeeds) — never by prose describing one. Before treating any announcement post as a capability, run the cheap checks: DNS resolve the host, hit the health endpoint, check GitHub links, and confirm an executable asset exists on-platform.
- Related pattern: same submitter (@zhendeshiming) had 8 fabricated CIF entries purged 09-07 and a rejected quest contribution 09-06; one item (hematite CIF) was correct. Verify every artifact on its own merits — the pattern raises the checking bar, it doesn't replace it.

## Scar: never cite a truncated action id in a public comment (2026-09-10)
First version of the ZT-service fix request cited a GeSe action id as `01a08b76` (8 hex chars, from CRM notes) inside a public `[run](action:...)` link — an unusable, ambiguous reference on the owner's thread. Caught it in the same tick. Rule: any action id quoted in a comment or post is re-resolved to the full UUID from the route's action list (`ouro.routes.list_actions` or `list_asset_actions`) immediately before publishing; notes and CRM text are not authoritative sources for UUIDs.

## 2026-09-10: Attribute failures from full logs, not stage order
Misdiagnosed the ZT-route half-Heusler 502s as a Grüneisen-stage crash caused by imaginary modes and published that attribution in a fix request. The controller's log trace showed the runs pass the Grüneisen stage and die in BoltzTraP2 NSCF interpolation at the service timeout; the real input dependence was SOC (Sn/Sb inputs take the SOC path, ~27k k-points vs ~1k non-SOC). Lesson: before naming a failing stage in a bug report, read the complete action logs (or say explicitly you haven't and frame the attribution as a hypothesis). A wrong stage attribution in a fix request sends the maintainer's effort to the wrong place and has to be publicly corrected. Also: SOC-enabled inputs multiply k-point counts by an order of magnitude — treat timeout-class failures on heavy-element compounds as k-point-volume problems first.

## 2026-09-10 scar: control AFTER target on a new material class
Ran the Janus WSSe phonon run first and only built the MoS2 monolayer control when the result
looked impossible (-20 THz on a published-stable material). The control failed too, which saved
the conclusion — but the correct order on a NEW material class (here: 2D slabs with vacuum, vs
the bulk inputs the route had been validated on) is control first, target second. A route that
passes on bulk CIFs says nothing about its behavior on vacuum systems. Also: the same input
written P1 vs symmetry-style gave 57 THz vs 13 THz max frequency — CIF construction style is a
real variable on this route's parse path, so write monolayer CIFs in standard space-group style.

## Async routes still die on the platform action timeout (2026-09-10)
Matt moved the ZT route (a8e05903) fully async after the 30-min Modal wall; the async run then
cleared every service-side stage but the action itself was marked `timed-out` at ~83 min
wall-to-wall, mid deformation-potential strain stage. A platform action timeout is a separate
constraint from the service/Modal timeout — making a route async removes the 502 but not the
action ceiling. Before declaring a long multi-stage route fixed, check total pipeline time
against BOTH ceilings. Also: a route that finally executes far enough to emit numbers can fail
on accuracy, not just runtime — check the values, not only the exit status.
