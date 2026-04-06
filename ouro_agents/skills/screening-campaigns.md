---
description: Run multi-step materials screening campaigns with tracked datasets, gated route chains, and ouro-py batch execution
load: stub
---

# Screening Campaigns

Use this pattern when you need to screen many candidate materials through a fixed computational workflow and keep results resumable across heartbeats.

## When To Use

- You have a candidate list and want a repeatable pipeline like `generate -> relax -> predict -> score`.
- You need to skip downstream work when upstream outputs fail validation.
- You want a durable Ouro dataset that tracks progress over time instead of scattering results across posts.

## Recommended Execution Pattern

1. Use the `developer` subagent for the stateful workflow and Ouro SDK access.
2. Keep one tracking dataset for the campaign.
3. Process candidates in small batches so a single heartbeat can make bounded progress.
4. Write one row per candidate per attempt, with enough metadata to resume cleanly.

Use delegation like:

```json
[{
  "subagent": "developer",
  "task": "Run the permanent-magnet screening batch for the next 10 candidates, update the tracking dataset, and summarize any promising hits."
}]
```

## Tracking Dataset

Prefer a single append-friendly dataset with columns like:

| column | purpose |
|---|---|
| `campaign_id` | Stable identifier for the screening effort |
| `candidate_id` | Composition or structure identifier |
| `status` | `pending`, `running`, `passed`, `failed`, `error`, `complete` |
| `current_stage` | `generate`, `relax`, `predict`, `score` |
| `attempt` | Retry counter |
| `generated_asset_id` | Upstream structure/file/route output |
| `relaxed_asset_id` | Relaxed output reference |
| `prediction` | Main predicted property |
| `score` | Final ranking score |
| `failure_reason` | Threshold miss, timeout, bad structure, etc. |
| `notes` | Short audit trail |
| `last_updated` | ISO timestamp |

## Gating Rules

Do not blindly run the full stack for every candidate.

- If generation fails or returns invalid output, mark the candidate failed and stop there.
- If relaxation diverges or quality checks fail, stop before prediction.
- If the prediction target falls below the screening threshold, skip scoring and mark why.
- Retry transient failures a small number of times, but do not loop forever.

Record the stage and reason every time you stop early so later heartbeats can distinguish real failures from unprocessed rows.

## Ouro SDK Pattern

Inside the `developer` subagent, use `run_python` plus `get_ouro_client()` for the full workflow. Keep route execution, dataset updates, and progress logging in one Python session when possible.

```python
from datetime import datetime, timezone

ouro = get_ouro_client()

CAMPAIGN_ID = "magnet-screen-v1"
DATASET_ID = "<tracking-dataset-id>"
MAX_RETRIES = 2


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def append_rows(rows):
    if not rows:
        return
    ouro.datasets.update(
        id=DATASET_ID,
        data=rows,
        data_mode="append",
    )


def execute_route(name_or_id, payload):
    return ouro.routes.use(
        name_or_id,
        body=payload,
        wait=True,
        poll_interval=5.0,
        poll_timeout=120.0,
    )


def score_candidate(prediction):
    return prediction.get("tc_k", 0) - 0.1 * prediction.get("energy_above_hull", 0)


results = []
for candidate in batch_candidates:
    row = {
        "campaign_id": CAMPAIGN_ID,
        "candidate_id": candidate["id"],
        "attempt": candidate.get("attempt", 1),
        "status": "running",
        "current_stage": "generate",
        "last_updated": now_iso(),
    }

    try:
        generated = execute_route("generate-structure", {"composition": candidate["composition"]})
        if not generated or not generated.get("success"):
            row.update(status="failed", failure_reason="generation_failed")
            results.append(row)
            continue

        row["generated_asset_id"] = generated.get("asset_id")
        row["current_stage"] = "relax"

        relaxed = execute_route("relax-structure", {"input_asset_id": row["generated_asset_id"]})
        if not relaxed or not relaxed.get("success"):
            row.update(status="failed", failure_reason="relaxation_failed")
            results.append(row)
            continue

        row["relaxed_asset_id"] = relaxed.get("asset_id")
        row["current_stage"] = "predict"

        predicted = execute_route("predict-tc", {"input_asset_id": row["relaxed_asset_id"]})
        if not predicted or not predicted.get("success"):
            row.update(status="failed", failure_reason="prediction_failed")
            results.append(row)
            continue

        tc_k = predicted.get("tc_k")
        row["prediction"] = tc_k
        if tc_k is None or tc_k < 300:
            row.update(status="failed", failure_reason="below_threshold")
            results.append(row)
            continue

        row["current_stage"] = "score"
        row["score"] = score_candidate(predicted)
        row["status"] = "complete"
        results.append(row)
    except Exception as exc:
        row.update(status="error", failure_reason=str(exc)[:200])
        results.append(row)

append_rows(results)
print(f"Processed {len(results)} candidates")
```

## Heartbeat-Friendly Cadence

- Start each heartbeat by querying the tracking dataset for `pending`, `error`, or retryable rows.
- Claim a small batch, run it, append results, and stop after one meaningful slice of progress.
- Publish a short summary post only when there is a material update: new top candidates, systematic failures, or threshold changes.

## What Good Outputs Look Like

- A tracking dataset that can answer "what ran, what failed, and what is next?"
- Short benchmark or progress posts that link back to the dataset.
- No hidden state in scratch notes that another heartbeat cannot reconstruct.
