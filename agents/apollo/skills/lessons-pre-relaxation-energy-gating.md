---
description: Learned lessons about pre-relaxation-energy-gating (distilled from memory)
load: stub
---

# Lessons: pre-relaxation-energy-gating

- Before relaxation, compute a single-point energy for the input geometry and reject it when the per-atom energy is orders of magnitude above the expected equilibrium range for its structure type. Calibrate the threshold against validated and deliberately wrong reference geometries, and verify that known valid cases survive while collapsed cases are rejected.
- When screening structures for MLIP symmetry-erasure risk, inspect Wyckoff free internal coordinates—especially free z-parameters—rather than relying on space-group category alone.
- Validate a "known anchor" CIF exactly like any other input before consuming it through a pipeline: the April τ-MnAl calibration CIF (21842dae) was labeled tau-phase but contained a 2-atom B2-topology cell (Mn 0,0,0 / Al ½,½,½; ρ 2.45 vs ~4.95 g/cm³) and anchored a published "chain REJECTs the textbook hard magnet" conclusion plus a bias-correction protocol. Note the energy gate alone would NOT have caught this one — the atoms sit in reasonable positions; the cell composition is what is wrong. Site count, density vs literature, and coordination/topology checks are the catch.
- Per-structure-type sanity: an L1₀ fct cell has 4 sites (2 per sublattice planes). A "prototype" CIF whose site count or density is 2× off the prototype is a different structure type, whatever its filename says.
