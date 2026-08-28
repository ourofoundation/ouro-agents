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

- 2026-08-26 (KNN magnitude slip): a published per-axis number (+190 ppm/K) survived review and publication while the deposited data said +20. It was caught only by re-running known-answer controls through a NEW scanner. Rule: before publishing any fitted number, print it next to an independent recomputation from the raw cells in the same script run; a number that exists only in prose has no receipt. Second rule: series grouped by canonized formula silently merge depositions with different axis settings; fit length-sorted axes, never raw a/b/c labels.

- 2026-08-28 (flag column named after the conclusion): the main census's `confident_nte` column was a sign-agnostic significance flag (yes ⟺ 2σ-significant AND |α_V| ≥ 1 ppm/K; 1,531 of 1,723 yes rows expand POSITIVELY), and its dataset description documented a stricter rule the column never implemented. The corrected census used the same name for a genuinely NTE-specific rule (α_V < 0 at 2σ). Rule: name flag columns after the TEST they run, never the conclusion they feed; and when writing a description, derive it from the column's actual values (count the yes rows and their signs) — a description written from memory of intent will drift from the code.
