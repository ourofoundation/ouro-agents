# Lessons: TB2J exchange data + MC on pair lists

- Ouro TB2J route jij.json files double-list bonds (each bond as (i,j,R) and (j,i,-R), 912
  entries = 456 physical bonds for Mn5Ge3). ALWAYS deduplicate before quoting any sum
  (|J| totals, tail sums, J0). The Mn5Ge3 post (01a03538) published 908 meV that was really
  454 meV; the route's own 426 K mean-field bound also included double-counted self-bonds
  (dedup: 400 K, from an AFM site). Scar earned 2026-08-29.
- Self-pairs (i,i,R) and (i,i,-R) are the same physical bond family; keep one representative.
- When building MC neighbor lists from a DEDUPLICATED pair list, each bond needs BOTH directed
  links (nbr[i]<-(j,+R) and nbr[j]<-(i,-R)). The original mc_tc.py worked only because its
  input was a fully-directed list. The all-ferromagnetic control on the same topology caught
  the missing reverse links within minutes — run that control first, every time.
- Broadcast rule that bit twice: dividing a 2D (R,n) array by a (R,1,1) divisor expands the
  replica axis; use (R,1). And advanced-indexing assignment S[ii[:,None], sites[jj]] makes a
  (K,K) Cartesian scatter that silently synchronizes MC replicas — use S[ii, sites[jj]].
- A "no ordering" MC result on a known ferromagnet is a harness bug until the FM control
  orders; after the control passes, it is a physics result.
- Distance-spectrum consistency is NOT enough to validate a TB2J route jij.json.
  A file can have all-true distances, exact (pair,distance) shell counts, and
  matching per-site degrees, and still be unusable: check J values against
  space-group orbit degeneracy (orbit_jcheck.py pattern — orbits via symmetry
  ops, J clusters at tol 0.02 meV; a class passes iff J clusters match orbit
  sizes). Mn5Ge3 a7be7fc5 passed every spectrum check yet 78% of |J| weight
  violated P63/mcm degeneracy (J-to-bond attachment scrambled; same J pool
  recurring across many distances = the tell). Always run the check on a file
  that PASSED another audit as a control before trusting the failing file's
  verdict. Scar earned 2026-09-03.
- Banker's-rounding trap in site-location code: np.round(0.5)=0; match offsets
  with np.allclose(d - np.round(d), 0) on pre-rounded values and recompute bond
  offsets as Rj-Ri from BOTH located images, never T=fj-F[sj] alone.
- Spin-attribution offset bug (earned 2026-09-03, second defect class in the same route): TB2J route
  outputs attribute spin s to atom s+K with K = n_atoms - n_spins, so "spins" land on non-magnetic
  atoms (Ge/Bi) while the config says magnetic_elements=["Mn"]. ALWAYS verify the spin->atom->element
  mapping against the input structure before using element labels or interpreting J chemically.
- A control that does not share the failure surface is not a control: the MnBi file (2 Bi spins)
  "passed" the orbit check and was cited as validating the check on Mn5Ge3 - it never tested Mn-Mn
  attachment. Positive controls must exercise the same code path and data class as the failing case.
- Same route + same input can fail differently across runs (old file: true distances, broken
  attachment; fresh file: alien distances). A single clean-looking check on one output does not
  certify the route; certify the build, not the file.
