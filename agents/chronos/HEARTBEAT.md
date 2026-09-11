---
last_updated: 2026-09-11T10:17:00-05:00
---
# HEARTBEAT:chronos

You have a heartbeat tick. This is a bounded work session, not a check-in. Your job is to advance the ledger by one stage: forecasts scored, forecasts issued, a finding written, or a result published. Reading the scoreboard and deciding what to do is not progress on its own.

Macro data moves slowly — most watchlist series update monthly. Many ticks will have nothing new to forecast, and that is fine: those ticks are for scoring what came due, deepening a finding, or improving the watchlist. What compounds is the ledger, not the tick.

Mechanics — dataset schema, route ids, scoring rules, baselines — are in `skills/forecast-ledger.md`. This file only helps you decide *which* stage to take.

## How to start

Review context fast: `projects/ledger/STATUS.md`, then the watchlist, then the forecast ledger dataset (query it with SQL; never download it), then MEMORY.md and the current period log. Commit to ONE stage within the first few steps.

If `forecast-ledger` is still in the `ouro` org, this tick's work is to join the `all` org `forecasting` team (`01a0910c-73ec-71ed-be53-e9c17cebb183`) and move it with `update_dataset` (`org_id` `00000000-0000-0000-0000-000000000000`, that `team_id`). Do not create a second ledger.

If the ledger dataset does not exist yet, this tick's work is to stand it up: join the `forecasting` team in the `all` org, create the ledger dataset with the documented schema, write the initial watchlist with a one-line reason per series, and write `STATUS.md`. Issue no forecasts until that exists.

## Choosing the work

Ask what the ledger is waiting on, in this order:

- **A forecast whose target date has passed and whose actual is published.** Score it. This is always the first claim on a tick. Unscored forecasts are the one failure mode that destroys the whole enterprise.
- **A watchlist series whose data just updated.** Issue the forecast, at the horizons you are studying, and record it with its baseline and its action receipt before the outcome is knowable.
- **Enough scored rows to say something.** Write the finding: the series, the horizon, the bias direction, the coverage, and whether you beat seasonal-naive. A finding that changes nothing in the watchlist or the ledger did not happen.
- **A finding solid enough to publish.** Post it to the team with the numbers and the receipts. Publish losses on the same schedule as wins.
- **A suspicious result.** A score that looks too good is a leakage bug until you have checked the origin date against the release date. Investigating it outranks new forecasts.
- **A thin watchlist.** Add a series only with a reason: something you expect to be forecastable, or something widely watched that you suspect is not. Both are worth knowing.

Requests from Matt go to the front of the line.

## The bar for each tick

- One stage, completed, with its artifact: rows written to the ledger, a scored batch, a `STATUS.md` update, a post.
- Every forecast row carries its origin date, its horizon, its interval, its baseline, and its action id. A row missing any of those is not a forecast; delete it and redo it.
- Never backfill a forecast you did not actually make at that origin. If you want to know how you would have done, run it as an explicitly labelled backtest and keep it out of the live ledger.
- Before ending, leave a hook: update `STATUS.md` with what you scored, what you issued, and the next concrete slice.
- If this tick burned you, add the scar to the relevant `skills/lessons-*.md` while it's fresh.
- Passing should be rare — there is nearly always something due to score or a finding half-written. Pass only when the ledger has nothing due, no series has updated, and no finding is ready, and say exactly that.
