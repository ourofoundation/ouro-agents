# Lessons: magCIF seeds and the Magnetic moments DFT route

- The `Magnetic moments` route (0a23817e, ABACUS DFT service) **stripped magCIF
  `_atom_site_moment_*` values before SCF**. Proven 2026-09-04 by twin-seed experiment:
  AFM-seeded (+2/-2 uB) and FM-seeded (+2/+2 uB) distorted Ni2O2 cells converge to
  byte-identical output (0.5157/0.2303 uB) on fresh SCF runs, and the FM twin even hits
  the AFM run's cache entry (cache key excludes moments). Files 30a9a4bb / 31c9eef0;
  actions 01a06d7d, 01a06d80, 01a06d9c, 01a06d9b.
- **FIXED same day by @mmoderwell** (see comment 01a06db5). Four route bugs, not one:
  (1) unconditional `primitive=True` in the shared CIF parser; (2) site properties
  dropped on rebuild (the actual stripping bug); (3) per-ELEMENT seeding in the STRU
  writer, so an "AFM seed" was really a uniform FM guess; (4) the symmetry-disable flag
  derived from element list, not resolved per-site moments, so symmetry detection would
  have averaged a held seed back together. With fixes deployed the seed is held end to
  end; the response now carries a `magnetic_sublattice` block
  (`n_magnetic_sites`, `initial_magmoms_uB`, `seed_is_antiparallel`,
  `ordering_representable`), plus an `initial_magmoms` request field for non-magCIF
  seeding. `reduce_to_primitive` defaults to false.
- Check that would have caught it earlier: before burning runs on mixer/beta variants,
  test whether the input knob of interest survives at all - run two inputs that differ
  ONLY in that knob and diff the outputs (and cache keys). Identical output from
  opposite inputs = the knob is stripped upstream, not "SCF is insensitive". (Note: the
  cache-key exclusion of moments was NOT explicitly among the four fixes; the
  twin-input diff check still applies to any new knob.)
- Residue is physics, not plumbing: on the fixed route the NiO negative control still
  fails because plain PBE without a Hubbard U collapses NiO (Mott insulator) toward a
  low-spin nearly-metallic solution. Route has no `dft_plus_u`. Do NOT read a
  same-sign/quenched AFM result as "the seed was ignored" - distinguish by checking
  `seed_is_antiparallel`/`ordering_representable` first, then functional limits.
- Consequence for ordering gates (semantics v2): FM verdict via signed moments counts
  only when `ordering_representable` AND `seed_is_antiparallel` are both true in the
  response; single-magnetic-site cells are UNRESOLVABLE, not passed. Same-settings
  negative control remains mandatory for any ordering claim.
- MAE/TB2J path still seeds per element through a legacy calculator: never read an MAE
  number for an antiferromagnet as a tier-2 gate until per-site seeds reach that path.
- Trust but verify route fixes: re-run your own control artifact through the updated
  route with defaults only (no hand-passed seeds) so the fix is tested, not assumed.

## Resolution (2026-09-04 19:30 UTC): route fix shipped, control still fails at defaults
- @mmoderwell shipped the "Magnetic ordering (FM vs AFM)" route (67393536) within hours of the OQ1 report: signed per-atom `initial_magmoms`, symmetry disabled on seeding, decision by energy comparison across enumerated orderings with `seed_held`/`converged_ordering` per config. The plumbing fix is real: the FM-seeded twin run proves seeds reach SCF on 8-atom supercells and the winner is classified by converged state, not seed label.
- New scar (classification vs physics): Matt's first-version run returned "AFM" on my NiO control, but the winning configuration had CONVERGED FM (same-sign +0.50/+0.23 uB) and was labeled by its seed. Verdicts derived from a configuration's seed label rather than its converged state inherit the collapse silently. Check: on any ordering decision, verify the winning config's converged_ordering and seed_held; a sub-1 meV/atom margin from a seed-collapsed config is noise, not evidence.
- New scar (control status): at default settings (beta 0.4, PBE, DZP, k 0.3) the route still fails the NiO known-answer control - FM-seeded twin returns FM; only 8-atom supercell AFM configs hold their seed and they sit 8.7-14.9 meV/atom higher. Collapsed state (+0.5 uB Ni, vs experimental 1.6-1.9) is an undermomented SCF endpoint, not physics. Recommended fix communicated (comment 01a06de4): auto-retry collapsed winners at beta 0.2/0.1 + "collapsed winner" warning.
- Infra scar: route re-run errored pre-compute with `AuthenticationError: Email link is invalid or has expired` in the Modal app's Ouro API-key exchange (modal_app.py). Infra, not science; report route-execution auth errors separately from route-logic findings.

- (2026-09-04) DFT+U on small transition-metal-oxide cells can fail SCF by pure charge sloshing (total charge oscillating at the 1e-6 level for hundreds of steps) even at beta 0.1 with 300 steps, where plain PBE converged on the same cell. Dropping beta alone does not fix it; the damping problem is the spin/density mixing scheme. Next lever: kerker or local-TF mixing, then fixed-spin-moment at the experimental moment as a sanity anchor.
- Pre-registered retries should still be capped: after two failed attempts on the same logical test (beta 0.2, then beta 0.1), stop launching and report the failure mode with receipts. A third tuning guess is the route owner's job, not the tester's.

- (2026-09-07) Type-II NiO AFM cell geometry for control design: q=(1/2,1/2,1/2) in cubic
  units. The 8-atom conventional cell embeds Type-II as [+,-,-,-] (uncompensated, net -4
  uB at 2 uB seeds) and NO seed on it can be compensated. A 2x1x1 supercell of the
  CONVENTIONAL cell (16 atoms, 8 Ni) is still uncompensated (2 up / 6 down, net -8 uB).
  Minimal compensated embedding: 2x1x1 of the PRIMITIVE fcc cell (4 atoms, 2 Ni, seed
  [+2,-2] uB, net 0); 2x2x1 conventional (32 atoms, 16 Ni) also works. Verified in
  pymatgen before posting the correction on magnes's Fe-W control post (comment
  01a07e56). Check: compute per-Ni sign of cos(2*pi*q.r) in cubic coordinates, don't
  assume a supercell shape compensates - count even/odd (111) planes.

- (2026-09-08) Ordering-route service drift caught by a known-answer control, not by my own pipeline: the NiO control file 30a9a4bb that ran successfully 09-04 is now rejected pre-compute by a new geometry gate (Ni-O 1.363 A), and two NEW pymatgen crash modes appeared on a clean moment-free bcc-Fe control: (1) FM strategy -> CollinearMagneticStructureAnalyzer "moments on both magmom site properties and spin species" (hypothesis: element-default seeding + attached magmom site property collide only for moment-free CIFs); (2) AFM/ferrimagnetic strategies -> MagneticStructureEnumerator `_remove_dummy_species: found neighbors=[]`. Reported with receipts (comment 01a081af on route 67393536). Check: re-run a previously-successful control file whenever a route's behavior changes; a control passing once is not a control that passes now.
- Sign-inversion catch: the two-seed envelope's first draft had gap = E_fm - E_alt but branches written as if positive meant "FM lower". Unit tests against the recorded NiO winners (gap +0.793 meV -> seed_collapse_risk) caught it before publish. Check: any signed-difference classifier gets a known-answer test on real recorded data before it ships.
