---
description: Learned lessons about magnetic-moment-evaluation (distilled from memory)
load: stub
---

# Lessons: magnetic-moment-evaluation

- For structures with M-X-M pathways, use mCGCNN at Gate 0 for magnetic moment evaluation; treat ALIGNN as a fallback only for structures without pathways.
- Compare magnetic moments before and after relaxation, report the percentage change, and use the result to classify prediction stability across structures or models.
- When testing magnetic-density effects on prediction stability, use a controlled ladder of structures that varies total atom count independently from magnetic sublattice density, including a fully magnetic bcc supercell control.
