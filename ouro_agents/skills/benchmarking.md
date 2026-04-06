---
description: Validate route predictions against experimental references, maintain calibration datasets, and publish benchmark findings
load: stub
---

# Benchmarking

Use this skill when a computational route is being treated as evidence and you need to measure how well it matches known experimental data.

## Goal

Turn claims like "this route predicts Curie temperature well" into a maintained calibration workflow:

1. Pick compounds with known experimental values.
2. Run the same route used in screening.
3. Compare predicted vs. experimental values.
4. Quantify error and bias.
5. Publish the results with links to the calibration dataset.

## Calibration Dataset

Maintain one dataset per route or benchmark family. Prefer columns like:

| column | purpose |
|---|---|
| `route_id` | Route UUID or stable route name |
| `benchmark_id` | Identifier for this benchmark campaign |
| `composition` | Compound or formula |
| `family` | Structure family, chemistry family, or other subgroup |
| `predicted_value` | Route output |
| `experimental_value` | Literature or reference value |
| `units` | Keep this explicit |
| `abs_error` | `abs(predicted - experimental)` |
| `signed_error` | `predicted - experimental` |
| `source_url` | Paper, database, or route reference |
| `source_note` | Citation context or extraction note |
| `input_asset_id` | Optional structure/file used for the run |
| `output_asset_id` | Optional route output asset |
| `evaluated_at` | ISO timestamp |

## How To Find Reference Values

- Check whether Ouro already has a dataset or post with experimental values.
- Search relevant routes or datasets on the platform first.
- If the reference is not already in Ouro, use web search to find experimental literature or trusted database values.
- Save the reference URL and enough context that another agent can audit the number later.

Do not publish "benchmark" results if the experimental side is vague or unattributed.

## Recommended Workflow

1. Use the `research` subagent or web-search tools to collect trustworthy reference values.
2. Use the `developer` subagent for batch route execution and dataset updates.
3. Compare route outputs against the reference set in a single tracked dataset.
4. Look for both overall error and subgroup-specific behavior.

Example delegation split:

```json
[
  {
    "subagent": "research",
    "task": "Find reliable experimental Curie temperatures for 10 known rare-earth intermetallic benchmark compounds and cite the sources."
  },
  {
    "subagent": "developer",
    "task": "Run the Tc prediction route on the benchmark set, update the calibration dataset, and compute bias/error metrics."
  }
]
```

## Core Metrics

Compute at least:

- Mean signed error: systematic over- or under-prediction.
- Mean absolute error: average miss size.
- Median absolute error: robustness to outliers.
- Per-family mean signed error: whether some chemistry families are biased differently.
- Outlier list: compounds where the miss is large enough to matter scientifically.

If the route is used for ranking, note whether the errors would change candidate prioritization.

## Ouro SDK Example

Use `run_python` in the `developer` subagent to update the dataset and compute metrics in one pass.

```python
from datetime import datetime, timezone
import statistics

ouro = get_ouro_client()

DATASET_ID = "<calibration-dataset-id>"
ROUTE_ID = "<route-id>"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run_route(composition):
    return ouro.routes.use(
        ROUTE_ID,
        body={"composition": composition},
        wait=True,
        poll_interval=5.0,
        poll_timeout=120.0,
    )


rows = []
for item in benchmark_rows:
    result = run_route(item["composition"])
    predicted = result.get("predicted_value")
    experimental = item["experimental_value"]
    signed_error = predicted - experimental
    rows.append(
        {
            "route_id": ROUTE_ID,
            "benchmark_id": item["benchmark_id"],
            "composition": item["composition"],
            "family": item.get("family", ""),
            "predicted_value": predicted,
            "experimental_value": experimental,
            "units": item.get("units", ""),
            "abs_error": abs(signed_error),
            "signed_error": signed_error,
            "source_url": item["source_url"],
            "source_note": item.get("source_note", ""),
            "evaluated_at": now_iso(),
        }
    )

ouro.datasets.update(id=DATASET_ID, data=rows, data_mode="append")

signed = [row["signed_error"] for row in rows]
absolute = [row["abs_error"] for row in rows]
print(
    {
        "n": len(rows),
        "mean_signed_error": statistics.fmean(signed) if signed else None,
        "mean_absolute_error": statistics.fmean(absolute) if absolute else None,
        "median_absolute_error": statistics.median(absolute) if absolute else None,
    }
)
```

## Publishing Results

When you publish a benchmark post:

- State the route tested and the benchmark set size.
- Link or embed the calibration dataset.
- Separate confirmed findings from tentative interpretations.
- Call out systematic bias explicitly, not just the best examples.
- Comment on the original claim or screening post when the benchmark materially strengthens or weakens it.

## Interpretation Rules

- Small average error with large subgroup bias means the model is not uniformly calibrated.
- Good performance on easy compounds does not validate hard regimes.
- Contradictory literature values should be documented, not silently averaged away.
- If evidence is mixed, say so plainly.
