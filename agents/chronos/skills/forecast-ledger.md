---
description: Mechanics of the forecast ledger — routes, dataset schema, baselines, scoring rules, and what each loop stage must leave behind
load: always
---

# Forecast ledger

One ledger dataset, one watchlist, one `projects/ledger/STATUS.md`. Every forecast you issue lands in the ledger before its outcome is knowable, and every ledger row is eventually scored against what happened. Nothing else you do matters if that invariant breaks.

Everything here lives in the `forecasting` team of the `ouro` org. Join it before your first write.

## Program files

`projects/ledger/STATUS.md` always contains:

- **Ledger dataset**: the dataset id. Copy it from tool output; never retype it.
- **Watchlist**: path to `projects/ledger/WATCHLIST.md`.
- **Horizons under study**: e.g. 1, 3, 6, 12 steps.
- **Scoreboard**: current per-series skill vs baseline, coverage, bias, and how many scored rows each is based on.
- **Due next**: the earliest unscored target date.
- **Next slice**: the exact thing the next tick does.

`projects/ledger/WATCHLIST.md` is one line per tracked series: source, series id, frequency, release lag, horizons, and one sentence on why it is worth tracking. A series with no reason does not belong on the watchlist.

## Routes

Execute via Ouro MCP `execute_route`. Inspect a route's schema with `get_asset(<route id>, detail="full")` the first time you use it.

| purpose | service | route |
|---|---|---|
| find a FRED series by title | Time Series Data | Fred Search |
| pull a FRED series' observations | Time Series Data | Fred Series |
| relative Google search interest | Time Series Data | Trends Interest Over Time |
| forecast a series | TimesFM | Forecast |

Notes that will save you a tick:

- **Fred Series** takes `units` (`lin` levels, `pch` percent change, `pc1` year-over-year) and `frequency` to aggregate down. Whatever you pass is what you are forecasting — record it, because a forecast of `pc1` scored against `lin` is meaningless.
- **Fred Series** returns `value: null` where FRED published nothing. A gap is not a zero and not a carry-forward.
- **Forecast** rejects histories containing gaps, by design. Decide explicitly: interpolate, start the history after the gap, or skip the series this tick. Record which you chose.
- **Forecast** infers frequency from `dates` and dates each step for you. Always pass `dates`; a forecast whose steps you have to date by hand is a forecast you will misalign.
- If you dropped observations to get around a gap, the remaining history is irregular and frequency inference fails — the steps come back with `date: null`. Pass `freq` explicitly (`MS` monthly, `QS` quarterly, `W-SUN` weekly) whenever you have touched the spacing. An undated forecast must not be written to the ledger.
- **Forecast** returns nine quantiles, `0.1` through `0.9`. The `0.1`/`0.9` pair is your 80% interval. Publish that pair and be judged on it.
- **Trends Interest Over Time** is unofficial and rate-limited; it fails with a 503 when Google refuses. That is an unavailable series for this tick, not a zero. The `is_partial` flag marks a period Google has not finished counting — never score against a partial period.

## Ledger dataset schema

Create it once with `create_dataset` in the `forecasting` team, named `forecast-ledger`. One row per forecast step.

| column | type | meaning |
|---|---|---|
| `run_id` | text | Groups all steps issued in one forecast call. |
| `source` | enum: `fred`, `trends` | Where the history came from. |
| `series_id` | text | `UNRATE`, `PCOPPUSDM`, or the Trends keyword. |
| `units` | text | The transformation the history was in (`lin`, `pc1`, `index`). |
| `origin_date` | date | Date of the last observation the model saw. |
| `issued_at` | timestamp | When you actually made the forecast. |
| `horizon_step` | integer | 1-based steps ahead of the origin. |
| `target_date` | date | The period being forecast. |
| `median` | float | The 0.5 quantile. |
| `q10` | float | Lower bound of the 80% interval. |
| `q90` | float | Upper bound of the 80% interval. |
| `baseline` | float | Seasonal-naive prediction for the same target. |
| `baseline_method` | text | e.g. `seasonal_naive_12`, `naive_last`. |
| `model` | text | `google/timesfm-3.0-pytorch`. |
| `action_id` | reference (`action`) | The forecast route's action receipt. |
| `realized_value` | float | The actual, once published. Null until scored. |
| `scored_at` | timestamp | When you scored it. Null until scored. |
| `abs_error` | float | `abs(median - realized_value)`. |
| `baseline_abs_error` | float | `abs(baseline - realized_value)`. |
| `inside_80` | enum: `yes`, `no` | Whether the actual fell in `[q10, q90]`. |
| `status` | enum: `open`, `scored`, `void` | `void` for rows invalidated by a data revision or a bug, with a reason. |
| `notes` | text | Why a row is void, which gap decision you made, anything a future tick needs. |

Query the ledger with SQL through `query_dataset`. Do not download it; the whole point is that it grows.

## Baselines

Every forecast is issued with a baseline for the same series, origin, and horizon, computed by you from the same history:

- **Seasonal-naive** for seasonal series: the value from one full season back (12 for monthly, 4 for quarterly, 52 for weekly Trends data).
- **Naive-last** for series with no meaningful season: the last observation, carried flat.

Pick one per series, record it in the watchlist, and do not switch it after the fact. Skill is `1 - mean(abs_error) / mean(baseline_abs_error)` over a set of scored rows: positive means you beat the baseline, zero means you are it, negative means the baseline is better and you should say so.

## Stage: score what came due

1. Query the ledger for rows with `status = 'open'` and `target_date` in the past.
2. Re-pull the series from its route, with the **same `units` and `frequency`** as the original forecast.
3. For each row with a published actual: write `realized_value`, `abs_error`, `baseline_abs_error`, `inside_80`, `scored_at`, and set `status = 'scored'`.
4. Leave rows open where the actual is not published yet — release lag is normal, absence is not a failure.
5. Leakage check before you believe a good score: was `issued_at` genuinely before the release date of the actual? If not, void the row with the reason. A forecast made after the value was published is not a forecast.
6. Revision check: if a previously scored actual has changed, keep the original score and note the revision. Do not silently rescore history.

## Stage: issue forecasts

1. Pull the history at the units you intend to forecast, with enough context — several seasons at minimum, the full series when it is short enough.
2. Handle any gaps deliberately and record the choice.
3. Compute the baseline for every horizon step.
4. Call the Forecast route once per series, passing `dates`, `horizon` (the longest horizon under study), and a `name`.
5. Write one row per step with the action id, before the outcome is knowable. A forecast that exists only in a post is not in the ledger.

## Stage: write a finding

A finding needs enough scored rows to not be noise — as a rule, at least ten scored targets for a given series and horizon before you claim skill or its absence, and say how many you have. State:

- The series, the horizon, and the number of scored rows.
- Skill vs baseline, and which direction.
- Coverage: the fraction inside the 80% interval, against the nominal 0.8.
- Bias: mean signed error, and which way it leans.
- What you will do differently: a horizon dropped, a series retired, an interval you no longer trust.

Publish findings to the `forecasting` team with the numbers inline and the ledger rows and action receipts linked. Publish the ones where you lost to the baseline on the same schedule as the ones where you won — that symmetry is the only thing that makes the wins credible.
