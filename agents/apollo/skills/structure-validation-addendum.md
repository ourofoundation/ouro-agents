---
description: Symmetry stability probing via the probe-cif-symmetry coil (addendum to structure-validation)
extends: structure-validation
load: stub
---

# Symmetry probing (coil)

When a task needs a CIF's symmetry checked — before MLIP runs, relaxation, Wyckoff analysis, or comparing orbit labels across sources — use the probe coil instead of hand-rolling a symprec scan:

- **When:** any "is this symmetry label trustworthy?" question; any CIF about to feed symmetry-sensitive downstream work; any suspicion of a broken/degraded file (P1 surprises, labels that differ between runs or between tools).
- **Coil:** `run_coil("probe-cif-symmetry", {"cif": <raw cif text>})` or `run_coil("probe-cif-symmetry", {"file_id": "<file asset uuid>"})`. Optional: `ref_symprec` (default 0.01), `noise_amp` (default ref_symprec/5), `noise_trials` (default 8). Pass exactly one of `cif`/`file_id`.
- **Returns:** `symprec_curve` over [0.001, 0.003, 0.01, 0.03, 0.05, 0.1], bounded coordinate-noise stability at the reference tolerance, and a plain `verdict.label`: `stable` | `tolerance-sensitive` | `noise-fragile` | `asymmetric-input` | `inconsistent` (or `ok: false` with a parse error — fail loud, no plausible garbage).
- **Published route:** [probe-cif-symmetry](route:fcef0bab-79e4-49b1-b891-af5c9d11dd40) on service apollo-routes (f0fe0b86-46a2-44dc-b181-e177f4ecdc60); anyone can call it via execute_route.
- **Regression receipts:** file asset e8cc87fd-592c-4b52-bc93-164400a5263c (offset-Si witness + degraded-Si edge, live actions 01a07e59 / 01a07e5a).

Prefer this over ad hoc `spglib.get_symmetry_dataset` loops. The verdict wording encodes the standing lessons: P1-everywhere means "broken file or genuinely asymmetric — do not trust downstream symmetry work" (the spinel-P1 incident), and a flip across the ladder means "quote symprec with every label" (the offset-Si 0.03/0.05 strip). Do not widen `noise_amp` past ref_symprec/2 to "test stability" — beyond that, detection failure is arithmetic, not fragility, and the verdict says so instead of crying fragile.
