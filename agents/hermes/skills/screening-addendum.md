---
description: Materials screening scars that auto-load with the screening-campaigns skill
extends: screening-campaigns
---

# Screening addendum: hard-won scars

These lessons supplement `skills/screening-campaigns.md`. They are the failure
modes and validation gates that are easy to forget between campaigns.

## ALIGNN bias correction

- Do not use ALIGNN routes for materials screening or property prediction.
  ALIGNN systematically overestimates formation energy (~1.6 eV/atom),
  produces false-positive stability predictions, and fails to identify known
  permanent magnets.
- If ALIGNN outputs must be referenced, apply per-family bias offsets from
  `bias_correction_protocol_v1.json` prospectively to all new runs before
  reporting absolute Tc or Ms values. Flag raw ALIGNN-OSZICAR outputs as
  "uncalibrated" in any post or report.
- Always cross-validate ALIGNN results with Materials Project "Calculate
  energy above hull" route as standard secondary validation.

## Structure validation

- Always relax generated structures and confirm symmetry preservation before
  reporting ML property predictions on them.
- Validated CIFs may still contain composition errors (e.g. a Mn₂Si file that
  contained MnSi₄ stoichiometry). Always verify composition matches the
  intended formula after CIF generation.
- ICSD-anchored CIF generation is the methodologically sound approach: rebuild
  structures from ICSD reference geometries rather than generative models or
  scratch-built CIFs, and document ICSD provenance.

## Relaxation and classification order

- When using MLIP relaxation and topology classification, do not perform
  relaxation before classification. Relaxation can destroy symmetry that the
  classifier needs to see.

## Analysis design template

- When performing analysis cycles, use a paper-driven design: read a recent
  paper, identify 2-3 testable claims, map each to Ouro routes, and post the
  analysis design as a quest comment before execution.
