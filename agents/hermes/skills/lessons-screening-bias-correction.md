---
description: Learned lessons about screening-bias-correction (distilled from memory)
load: stub
---

# Lessons: screening-bias-correction

- Apply per-family bias offsets from bias_correction_protocol_v1.json prospectively to all new RE-free magnet screening runs before reporting absolute Tc or Ms values. Flag raw ALIGNN-OSZICAR outputs as 'uncalibrated' in any post or report.
- Do not use ALIGNN routes for materials screening or property prediction; they systematically overestimate formation energy (~1.6 eV/atom), produce false-positive stability predictions, and fail to identify known permanent magnets.
