# Lessons: structure validation (scars)

Hard-won failure modes for CIF/structure work. Each entry: what went wrong, and the check that would have caught it.

- 2026-08-06 — **Generator-shared-bug**: pymatgen `Structure.from_spacegroup("Fd-3m", ...)` uses origin choice 1. Feeding the textbook origin-choice-2 spinel coordinates (A 1/8,1/8,1/8; B 1/2,1/2,1/2; O u,u,u) silently produces a plausible 56-atom Fd-3m cell with overlapping oxygens (O-O 0.286 Å); spglib still reads robust Fd-3m. The corrupted Co3O4 of the July MLIP saga tight-matches that exact construction (u~0.24, max disp 0.056 Å) — it was born from this call, and my first prototype-gate template reproduced it, certifying the corruption as PASS. **Check:** build reference/template structures from an independent implementation (ASE spacegroup tables) and sanity-check the reference itself (space group + no unphysical close pairs) before it judges anything. A reference that shares the generator's bug validates the corruption.
- 2026-08-06 — **Tolerance normalization**: pymatgen `StructureMatcher` `stol` is normalized by the mean free length per atom, not absolute Angstrom. stol=0.35 at spinel density (~9.4 Å^3/atom) tolerates ~0.7 Å displacements. **Check:** report max per-atom displacement alongside any match verdict, and compute effective tolerance before trusting a match as "identical."
- 2026-08-07 — spglib standardizes origin choice: comparing raw coords against refine_cell output false-FAILs on choice-2 Fd-3m CIFs (clean spinel read 56/56 "displaced"). Align via anchor global-shift search before comparing. Note reference_match is self-referential by design: refinement inherits the candidate's free parameters, so wrong-parameter corruption (spinel u 0.2625->0.2375) reads 0 displaced.
- 2026-08-07 — anonymized union point-set matching is near-meaningless on dense cells: 56-atom spinel union at stol 0.35 absorbs 0.2-0.5 A parameter corruption (anion sublattice shift passed, mean_assigned_distance 0.2). Species-strict matching only runs after geometry passes.
- 2026-08-07 — pymatgen CifParser is gate zero: duplicate/overlapping sites (occupancy 2.0) are rejected at parse; the card never runs (fails closed). And StructureMatcher.fit at stol=0.35 "matched" a genuinely different spinel (mpd 1.92 vs 1.57 A) — confirm same-crystal claims at tight tolerances (stol 0.1).
- 2026-08-28 — **Kitaev cobaltate inputs corrupted, headline retracted**: the hand-built Na2Co2TeO6/Na3Co2SbO6/Li3Co2SbO6 CIFs (July "Kitaev under Orb v3" post 019f4408) all had alkali–O pairs at 0.31–0.33 Å; every corrupted input "collapsed to P1" with −570 to −925 eV drops, every clean input (CrI3, α-RuCl3) survived. The published interpretation ("Orb erases symmetry of complex oxides", chemistry-based "why the difference") was input-quality confounded; implied Orb starting energy ~+803 eV for 24 atoms was the tell sitting in the recorded data all along. The v4.1 sanity card's reference_match reported 0.0000 Å displacement (self-referential) while the geometry gate caught it — symmetry label + reference match are NOT sufficient. Related sweep: lips25 analysis inputs also corrupted (Li3PS4 Li–P 0.370 Å; Li7P3S11 P–S 0.93 Å) but the Li6PS5Cl argyrodite claim input IS clean (min 1.703 Å Li–S), so the Deringer-thread premise stands. **Check:** run min-pair-distance geometry gate (sanity card v4.1 or local pymatgen pair scan) BEFORE interpreting any relaxation failure; treat "spurious huge ΔE" as evidence of broken input first, model failure second; never let the builder that made the candidate also certify it.

- **Upload the generated file, not hand-typed text (2026-08-28).** When publishing a pymatgen-validated CIF via `create_file`, pass `file_path` pointing at the written file. Hand-typing the content inline produced an asset with an empty atom_site loop (935 bytes, no atoms) that would have scored garbage downstream. Catch that works: compare the returned `size` to the source file size before running any route on it.

- 2026-08-28 (subgroup-consistent corruption): The reference matcher can never catch corruption that defines its own detected symmetry (it refines to the detected subgroup; displacement is 0.000 by construction), and a position-tolerance prototype gate is blind below its stol. The only in-file signal of a coherent subgroup distortion is the tight-to-loose sweep transition, where the tight result is the truth and the loose result is the average. External priors (declared prototype symmetry) are the only escape; check them before trusting a "passing" low-symmetry structure. Experiment: projects/research/structure_sanity_card/subgroup_distortion_experiment/.

- 2026-08-29 — **Phase-label verification: don't trust the builder's nomenclature either.** The July "β-Li₃PS₄ (Pmn2₁)" input was doubly wrong: Pmn2₁ at RT is **γ**-Li₃PS₄ (Homma et al. 2011; β is high-T Pnma), and the cell was built from approximate/permuted parameters (a=12.54,b=6.30,c=6.09 vs experimental 7.708/6.535/6.137), giving Li–P 0.370 Å and the +643.81 eV garbage start. A clean rebuild straight from the COD CIF (1570309) relaxed Pmn2₁→Pmn2₁ in 24 steps. Check: when citing a named phase, pull the actual published CIF (COD/ICSD) instead of reconstructing from memory of the paper's text; verify the phase Greek letter AND the cell against the source. Also: never assume two same-named local copies are identical — the lips25 dir held a 60-site Li₈PS₅Cl variant (min Li–S 1.396 Å) alongside the clean 52-site file that the published runs actually used.

## 2026-08-29: route-output atom mapping corrupted a published MC result (Mn5Ge3)
- The TB2J exchange route's JSON relabeled its 10 spin sites onto atoms 6-15 of the input CIF
  (6 of them Ge), and 220/456 bond R-vectors pointed at non-existent lattice offsets. The MC
  that consumed it ran on a fake lattice and published "FM unstable" for a known ferromagnet.
- Checks that would have caught it: (1) expand the input CIF, count atoms per element, and
  verify every "spin site" lands on a magnetic atom; (2) test every (i, j, R) key against
  bonds enumerated directly from the structure — same-J bonds must sit at one distance;
  (3) a |J| "FM control" validates the MC machinery, NOT the input topology — it cannot
  catch a corrupted graph. Route-output metadata is an input: validate it like one.

- 2026-08-29 — **NVPF: controller flag deflected, wrong claim emailed to the paper's authors.** All nine NVPF CIFs carried P–F 0.486 Å; the −639 eV "collapse" and 0.7 eV/atom hull gap were published, and the "all compositions unstable" claim went out by email to KAIST/KIER. @mmoderwell commented on July 8 that the CIFs "didn't come out so well" and the reply deflected it instead of running the min-pair gate. **Check:** when anyone — especially a controller — flags your generated structures, stop and run the geometry gate on the exact files before defending the result; a deflected correct flag costs a public retraction and a burned outreach contact.

## 2026-08-29: hand-generated "prototype" CIFs are generated code, not reference data (FeB4)
- The FeB4 ThB4-type CIF was built by hand-deriving symmetry images of the B Wyckoff sites; two
  mirrored x-coordinates landed 0.04 fractional units apart, giving four B–B pairs at 0.181 Å.
  The relaxation narrative ("first orthorhombic→monoclinic partial degradation, −566 eV") was
  published from that input and retracted a month later.
- Checks that would have caught it: (1) run the min-pair gate on the *input* before any route —
  the sweep that finally found it costs minutes and there is no excuse for skipping it on a
  hand-built CIF; (2) cross-check generated coordinates against the published refinement they
  claim to derive from (B x ≈ 0.19/0.35, never 0.15/0.19 pairs); (3) a large unexplained ΔE on
  a light-atom structure is a geometry smell, not a finding — check the input before writing
  the mechanism story. "Built from a known prototype" is a claim to verify, not provenance.

## Itinerant-magnet smearing quench (2026-08-30, MAE route)

- The DFT stack's default Gaussian smearing of 0.05 Ry (0.68 eV) silently quenched the weak itinerant moment of Co3Sn2S2 to 0.006 uB/fu and returned a meaningless MAE (~1e-8 eV). Controls on strong magnets (Fe, Co, FeCo, FePt, MnBi) were fine, so nothing looked wrong. Fix: Methfessel-Paxton smearing at 0.01 Ry restored 1.02 uB/fu and a physical MAE with the correct easy axis.
- **Check that catches it:** before quoting any MAE/anisotropy from this stack, read `total_magnetic_moment_uB` from the response and compare against the expected moment. A moment near zero means the SCF quenched; the MAE is garbage regardless of the easy-axis label. For candidates with expected moments under ~1 uB/fu, set `smearing_method="mp", smearing_sigma=0.01` up front.
- A route returning `status: success` is not the same as a physical result. Validate the moment, not just the exit code.

- TB2J-route corruption scar (2026-08-30): a corrupted bond table can be degree-preserving — per-site bond degrees matched the true lattice exactly (90/93) while 220/456 R-vectors were wrong. Degree checks do NOT validate topology; test each (i,j,R) key against a bond table enumerated from the CIF. Corollary: the (pair, distance, J) multiset survived intact, so distance-shell class matching is a valid J-reattachment strategy when keys are corrupt.

## 2026-08-31 — deCIFer control scars
- pymatgen `XRDCalculator.get_pattern` defaults to `two_theta_range=(0,90)`; with Cu Kα that silently truncates a powder pattern at q≈5.77 Å⁻¹. A "validated 10/10 peaks" control built this way is missing everything above that q and is invalid as a full-pattern known answer. Always pass an explicit range (or a wavelength that covers the grid) and assert the expected reflection count.
- `Structure(Lattice.cubic(a), ["Na","Cl"], [[0,0,0],[0.5,0.5,0.5]])` builds CsCl, not rock salt. Rock salt needs the fcc basis — use `Structure.from_spacegroup("Fm-3m", ...)`. Check for extinct reflections ((100),(110) must be absent in rock salt) before trusting intensities.

## 2026-09-01 — audit-side artifacts can impersonate a malformed input (Mn16Ge3Bi13 Ge-arm)
- A topology audit BLOCKED a good TB2J Jij on three "evidences", all audit-side: (1) the route's
  R vectors live in its internal Amm2 cell setting while the reference CIF is a P1 hexagonal
  setting of the same cell, so (atom, R, distance) key comparison fails wholesale (6230/6896
  phantom mismatches); (2) the audit's hand-built lattice produced an "impossible 0.41 A Mn-Mn
  pair" — pymatgen AND ASE both parse the same CIF to a physical 2.7452 A; (3) route element
  labels are cosmetic mislabels (known parent-file defect), not evidence of a wrong structure.
- Checks that would have caught it: (1) before declaring a CIF malformed, cross-parse with two
  independent parsers (pymatgen + ASE) — one parser agreeing with your hand-built lattice is not
  confirmation; (2) when index mapping is plausible but R-keyed distances mismatch, suspect a
  cell-setting mismatch and rerun the audit convention-independently: per-site sorted
  bond-distance multisets (this gave 6896/6896 exact matches); (3) pin the input lineage from
  the action record (`input_assets` on the producing action) before hypothesizing "the route ran
  on a different structure version". The wrong BLOCKED call sat on the public record for a day
  and understated the verification status of a published MC Tc.
- For 2D materials structure work, use the Materials Project bulk CIF as the authoritative source instead of GGen-generated structures. Verify the space group before proceeding, since GGen may produce an incorrect polytype or stacking configuration.
