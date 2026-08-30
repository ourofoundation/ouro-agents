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

- 2026-08-29 (stale spark vs live thread): an ideas.md spark dated 8/22 was already answered by a public correction comment posted hours later, and the file never tracked it — executing the spark cost a re-derivation. Rule: before working an ideas.md spark in a thread with corrections, read the thread's comments first; ideas.md records questions, not their answers. Silver lining: the independent recompute confirmed both corrections from raw and surfaced the quadrature/se_v posterior detector (ratio ~100 where independence predicts ~1 = per-axis channel dominated by V-invariant contamination, i.e. axis-setting mixing).

- 2026-08-30 (reference unit assumed, not read): the COD pressure census's central claim ("COD expects hPa; depositors used bar/MPa/GPa") was itself the artifact. The CIF core dictionary and COD document `_cod_cellpressure` in kPa, and the "impossible 20,000 GPa" values were my own bar-anchored conversion. Unit forensics then showed 59/94 series are standards-compliant. Rules: (1) before accusing depositors of a convention violation, read the dictionary definition of the column, the reference unit is a checkable fact and the check costs one page; (2) an anchor entry disambiguates units better than any fit: a near-ambient deposit pins the unit to ~1 decimal (101 kPa vs 1013 hPa vs 1 atm); (3) EOS fits alone cannot pin units that differ by 10-100x on soft solids, low-P data are scale-degenerate, so never classify a unit from fit quality alone; (4) external receipts should target thresholds, not maxima: a paper's reported transition pressure (44 kbar falling between two ladder points) validates a unit reading more strongly than a paraphrased "study maximum".

- 2026-08-30 (pressure census, atlas slice): DOI-level "series" counts conflate multiple compounds and settings aliases with real transitions — always regroup per compound (DOI+formula+Z) and compare space-group NUMBERS, not symbols, before claiming a phase transition.
- 2026-08-30 (unit solving): without a near-ambient anchor, soft solids are scale-degenerate under BM2+rms gating — the blind solver put acetonitrile at 57 GPa (paper: 0.63) and Pb2SnO4 at 1 GPa (literature: 10-12). When a data dictionary documents the unit (CIF kPa), that frame is the default hypothesis requiring positive evidence to override, not one candidate among eight.
