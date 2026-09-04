---
description: Mechanics of a Magnes discovery program — routes, tier gates, the candidates dataset schema, scoring, and what each loop stage must leave behind
load: always
---

# Discovery loop

One program = one goal, one `projects/<slug>/STATUS.md`, one candidates dataset, one control candidate, one ledger thread. The loop below runs inside a program. Stages are meant to fit a single heartbeat; a hypothesis spans several.

## Program files

`projects/<slug>/STATUS.md` must always contain:

- **Goal**: one sentence. Constraints (e.g. excluded elements).
- **Targets**: the property table below, with any program-specific overrides.
- **Dataset**: the candidates dataset id (never retype it; copy from tool output).
- **Control**: formula, CIF file id, expected values, date last run, last observed values.
- **Current hypothesis**: id, statement, falsifier, stage (`formed | explored | tier1 | tier2 | verdict`).
- **Next slice**: the exact thing the next tick does.

The research ledger (team `research-ledger.md`, per `research-program`) holds findings, open questions, and dead ends. Each hypothesis is an open question while live and becomes a finding or dead end at verdict.

## Stage 1: hypothesize

Write into `STATUS.md` and the ledger:

```
H<n>: <system or family> under <constraints> should raise <property> because <mechanism>.
Builds on: <finding/open question id>.
Falsified if: <concrete observable, e.g. "no near-hull phase with SG >= 8 and Ms > 0.8 T">.
Exploration: system=<A-B-C>, crystal_systems=[...], fraction bounds {...}, e_above_hull=<eV/atom>.
```

Novelty test (from `research-program`) applies. A system already in the dataset with the same constraints is not a new hypothesis.

## Stage 2: generate

Routes (execute via Ouro MCP `execute_route`; inspect the schema with `get_asset(route, detail="full")` the first time):

| purpose | route |
|---|---|
| explore a system | `mmoderwell/explore-a-chemical-system-with-ggen` |
| export near-hull CIFs when explore did not return them | `mmoderwell/export-candidate-cifs` |
| one structure for a given formula (rare; for controls or targeted checks) | `mmoderwell/generate-a-crystal-structure-using-ggen` |

Explore returns a summary of stable phases plus candidate CIFs (and `e_above_hull` per phase). Before any CIF enters the dataset: load `structure-validation` and check it (parses, composition matches, sensible density, space group recorded). Failed validation is a row with `status=failed`, `failure_reason=invalid_structure`. Record the exploration `action_id` on every row it produced.

Skip anything containing excluded elements (magnet program: La through Lu, Sc, Y).

## Stage 3: tier 1 (cheap, on every near-hull survivor)

| property | route | field | column |
|---|---|---|---|
| energy above hull | from explore output; else `mmoderwell/calculate-energy-above-hull` | `e_above_hull` | `e_hull_ev_atom` |
| saturation magnetization | `hermes/estimate-magnetic-moments-and-ms-from-a-cif` | `saturation_magnetization.tesla.value` | `ms_tesla` |
| Curie temperature | `hermes/predict-curie-temperature-from-a-cif` | `temperature` | `tc_k` |
| raw material cost | `hermes/estimate-raw-material-cost-per-kg` | `cost_per_kg.value` | `cost_usd_kg` |

Order matters: Ms first. A structure whose magnetic ground state is not ferromagnetic (Ms near zero, or the moments route reports AFM/PM) is out; do not run Tc on it, the Curie predictor gives spurious numbers on non-FM inputs (see Hermes' `lessons-screening-gate-order`).

## Tier-1 gates (all must pass to earn tier 2)

- `e_hull_ev_atom <= 0.150`
- `ms_tesla >= 0.10` (soft floor; anything below is not a magnet candidate)
- `space_group >= 8` (triclinic/monoclinic-P are synthesis red flags and rarely uniaxial)
- `num_atoms <= 30`
- dynamic stability: `mmoderwell/calculate-phonon-dispersion-and-return-band-structure-plot`, pass when `imaginary_modes_detected` is false. Run this last within tier 1 since it is the most expensive tier-1 step; only on rows that already pass the four above.

Rows that fail a gate get `status=gated`, `failure_reason=<gate>`, and are done. They still count as evidence for the verdict.

## Stage 4: tier 2 (expensive, only on gated-in candidates)

| property | route | body | field | column |
|---|---|---|---|---|
| magnetocrystalline anisotropy energy | `mmoderwell/calculate-magnetic-anisotropy-energy-mae` | `{"method":"tb2j","ecutwfc":65,"scf_thr":1e-6,"kspacing":0.16,"scf_nmax":200,"smearing_sigma_ev":0.05,"smearing_method":"mp"}` | `mae_mj_per_m3` | `mae_mj_m3` |

Rank tier-1 survivors by partial score first and run MAE on the top few, not all. Cap at one or two MAE runs per tick.

## Stage 5: score

Targets for the magnet program (override per program in `STATUS.md`):

| property | direction | target | weight |
|---|---|---|---|
| `e_hull_ev_atom` | lower | 0.150 | 0.18 |
| `ms_tesla` | higher | 0.10 | 0.18 |
| `mae_mj_m3` | higher | 1.5 | 0.18 |
| `tc_k` | higher | 500 | 0.18 |
| `cost_usd_kg` | lower | 100 | 0.13 |
| `dynamically_stable` | boolean | true | 0.13 |
| `num_atoms` | lower | 30 | 0.05 |
| `space_group` | penalty | >= 8 | 0.05 |

Soft saturation, computed in sandbox Python and written to `score`:

```python
import math

def score(row, targets):
    total = 0.0
    for prop, spec in targets.items():
        v = row.get(prop)
        if v is None:
            continue                       # missing contributes 0 but keeps its weight in the denominator
        if spec["dir"] == "lower":
            total += spec["w"] * math.exp(-v / spec["target"])
        elif spec["dir"] == "higher":
            total += spec["w"] * (1 - math.exp(-v / spec["target"]))
        elif spec["dir"] == "bool":
            total += spec["w"] * (1.0 if v else 0.0)
        elif spec["dir"] == "penalty":
            total += spec["w"] * (1.0 if v >= spec["target"] else v / spec["target"])
    return max(0.0, min(1.0, total))
```

A score is a ranking device within a hypothesis. Never report a score without the property values behind it.

## Stage 6: verdict

Write to the ledger and `STATUS.md`, then post:

- **Supported / Refuted / Inconclusive**, one sentence each on why and what the falsifier did.
- The best candidate(s) with full property table and receipts, or the statement that none passed the gates.
- What this implies for the next hypothesis (or that the line is dead and why).
- Tooling gaps encountered (hand to @apollo) and anything Hermes should carry.

Inconclusive twice in a row on the same mechanism means the tooling, not the chemistry, is the open question. Say so and stop the line.

## Candidates dataset

One dataset per program, one row per (candidate, attempt). Create with `create_dataset`; append with `update_dataset(data_mode="append")`. Query it; never download it.

| column | type | notes |
|---|---|---|
| `program` | text | program slug |
| `hypothesis_id` | text | `H<n>` |
| `formula` | text | reduced formula |
| `system` | text | e.g. `Fe-Co-Bi` |
| `cif_file_id` | reference (asset, file) | validated CIF |
| `space_group` | int | from validation |
| `num_atoms` | int | primitive cell |
| `e_hull_ev_atom` | float | |
| `ms_tesla` | float | |
| `tc_k` | float | |
| `cost_usd_kg` | float | |
| `dynamically_stable` | bool | null until phonons run |
| `mae_mj_m3` | float | null until tier 2 |
| `score` | float | |
| `stage` | enum | `generated, tier1, tier2, scored` |
| `status` | enum | `pending, passed, gated, failed, error, control` |
| `failure_reason` | text | gate name, `invalid_structure`, route error, timeout |
| `action_ids` | text | comma-separated action ids that produced values in this row |
| `notes` | text | one line, observation only |
| `updated_at` | timestamp | ISO |

Pass `refs={"cif_file_id": {"kind": "asset", "asset_type": "file"}}` and `enum_columns` for `stage` and `status` on creation.

## Control

Pick one real, well-characterized magnet the routes should get roughly right (an RE-free one, e.g. an Fe16N2 or MnBi phase, or alnico's Fe-Co base) and one that should fail the gates. Both are rows with `status=control`. Re-run tier 1 on them weekly and whenever a route's behavior changes. Expected vs observed lives in `STATUS.md`. Drift invalidates every result since the last good control run; that is a post before it is anything else.

## Execution pattern

For a batch (tier 1 on N rows, scoring, dataset append) delegate to `developer` with `run_python` and `get_ouro_client()`, following `screening-campaigns` for the resumable-batch shape. Keep hypothesis formation and verdicts in the parent; they are judgment, not plumbing.
