---
description: Validate crystal structures (CIFs) you build or receive before any MLIP, relaxation, or property-prediction work
load: stub
---

# Structure Validation

Run this checklist on every CIF you build, edit, or download **before** it enters any downstream computation (relaxation, MLIP benchmark, property prediction). A structure that fails validation invalidates everything computed from it. This is agent-driven: use your sandbox and existing platform routes; do not wait for the harness to check for you.

## Why this exists

A past benchmark concluded that MLIPs collapse spinels to P1. The real cause was hand-built CIFs with wrong site occupancies and broken symmetry — the structures were already wrong before the models ever saw them. Ten minutes of validation would have prevented a wrong published conclusion.

## The checklist

Run in sandbox Python with `pymatgen` (and `spglib` via `SpacegroupAnalyzer`):

```python
from pymatgen.core import Structure
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

s = Structure.from_file("candidate.cif")

report = {
    "formula": s.composition.reduced_formula,
    "n_sites": len(s),
    "volume_per_atom": s.volume / len(s),
    "density_g_cm3": float(s.density),
    "spacegroup": SpacegroupAnalyzer(s, symprec=0.1).get_space_group_symbol(),
    "min_pair_distance": min(
        s.get_distance(i, j) for i in range(len(s)) for j in range(i + 1, len(s))
    ),
    "occupancies_ok": all(site.is_ordered for site in s),
}
print(report)
```

Then check each item against intent:

1. **It parses.** If pymatgen can't read it, stop.
2. **Stoichiometry matches.** Reduced formula and site count match the phase you intended (e.g. MgAl2O4 spinel: 8 formula units, 56 atoms in the conventional cell).
3. **Spacegroup matches the prototype.** Analyze at a couple of `symprec` values (0.01, 0.1). If you built a spinel and the analyzer says P1, *your CIF is wrong* — do not proceed. Compare against the known prototype spacegroup (spinel Fd-3m, Heusler Fm-3m, C14 Laves P6_3/mmc, etc.).
4. **No overlapping or absurdly close sites.** Minimum pair distance below ~1.5 Å for non-hydrogen pairs almost always means duplicated or misplaced sites.
5. **Sensible density and volume.** Compare density against the known experimental value or a same-family compound; a factor-of-two miss means a wrong cell or wrong Z.
6. **Full occupancies** unless you deliberately built a disordered structure.
7. **Diff against a reference when one exists.** Prefer fetching the structure from an established source (Materials Project, COD, ICSD-derived data, or an existing validated Ouro file asset) over hand-writing a CIF. If you must hand-build, use `StructureMatcher` to compare against a reference of the same prototype.

## Validation of relaxed outputs

After relaxation, re-run the same analysis on the output and compare to the input:

- Spacegroup change: real relaxations can lower symmetry, but a collapse to P1 from a high-symmetry prototype is a red flag — check the input first, then the relaxer settings (symmetry tolerance, cell constraints).
- Volume change over ~15–20% or energy per atom that is wildly off known formation energies means something is broken, not discovered.

## Record it

Note the validation result (spacegroup found, checks passed) wherever you track the structure — the screening dataset row, the file asset description, or your run notes — so later heartbeats and other agents can trust the artifact without redoing the work. If a structure fails validation, mark it failed at that stage and do not pass it downstream (see `screening-campaigns` gating rules).
