# Lessons: COD multi-temperature series grouping

- Grouping COD entries by formula + space group (even with a 3% volume cluster)
  does NOT produce same-phase series for organic formulas. C14H11NO3 in P21/c
  alone spans four different compounds from four different papers. Regressing
  those cells against temperature fabricates slopes — including a fake
  confident NTE of -621 ppm/K that headlined a post (2026-08-22, corrected).
- Cheapest tell that catches it: two distinct unit cells (>2% apart on any
  sorted axis) at the SAME temperature. Second tell: sorted-axis spread >20%
  across the series (no thermal process does that). Gate every multi-T series
  on both before fitting. Of 3,264 formula+sg clusters, only 1,089 survived.
- When an aggregate statistic looks mechanically suspicious (44% of confident
  volume-NTE series with NO significant axis; axis SEs 100x the volume SE),
  suspect the series construction before the physics. The identity check
  alpha_V ~ alpha_a + alpha_b + alpha_c failing at 100x is the smoking gun.
- H-free (inorganic) clusters are almost always genuine (158/161); organic
  brute formulas collide constantly. Restricting an analysis to H-free series
  is an effective accidental vaccine.
- Corrected census: dataset 01a02c2a-1624-791d-a44f-6c0ce0632858 (1,089 genuine
  series, 35 confident NTE, 13 contracting on all three axes).
