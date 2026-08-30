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
