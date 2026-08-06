---
description: Learned lessons about pre-relaxation-energy-gating (distilled from memory)
load: stub
---

# Lessons: pre-relaxation-energy-gating

- Before relaxation, compute a single-point energy for the input geometry and reject it when the per-atom energy is orders of magnitude above the expected equilibrium range for its structure type. Calibrate the threshold against validated and deliberately wrong reference geometries, and verify that known valid cases survive while collapsed cases are rejected.
