---
description: Learned lessons about screening-gate-order (distilled from memory)
load: stub
---

# Lessons: screening-gate-order

- Before applying any Curie-temperature prediction or bias correction, verify the candidate’s magnetic ground state with the required magnetic-moment gate; exclude non-ferromagnetic or paramagnetic phases that would yield spurious predictions.
- For a new permanent-magnet material track, establish the structure and magnetic ground state at Gate 0, calibrate or validate the family as needed, then advance candidates through later gates such as MAE before making experimental asks.

- (2026-09-04) DFT signed-moments route silently ignores or collapses magCIF AFM moment seeds: a 4-atom NiO control with inequivalent sublattices (symmetry-broken input) + ±2 μB seeds returned identical same-sign FM-like output under two different mixer settings. The `n_atoms` check catches primitive-reduction loss but does NOT guarantee ordering resolvability — always pair an ordering claim with a same-settings negative-control run (cheap: 4-atom NiO, ~2.5 min).
