---
description: Epistemic ground rules for any scientific or computational claim — anomalies are bugs until proven otherwise, validate inputs before interpreting outputs
load: always
---

# Scientific Method

These rules apply whenever you run a computation and interpret its output as a finding — benchmarks, screenings, relaxations, property predictions, data analysis. They are cheap to follow and expensive to skip.

## An anomalous result is a bug until proven otherwise

When a result is surprising — a symmetry collapse, a wild property value, a model "failing" on an easy case — your first hypothesis is that *your pipeline* is broken, not that you discovered something. In practice, most surprising results trace to a malformed input, a unit mismatch, a wrong setting, or a misread output. Only after you have ruled those out does the result become interesting.

Concrete example of the failure mode: an agent benchmarked MLIPs on spinels, saw structures relaxing to P1, and concluded the models couldn't hold the symmetry. The actual cause was that the agent had built broken CIFs. The wrong conclusion was published; the input was never checked.

## Validate inputs before trusting outputs

Garbage in produces confident-looking garbage out. Before feeding a structure, dataset, or file into any downstream computation, verify it yourself using the tools you already have (sandbox Python, platform routes). For crystal structures, load the `structure-validation` skill and follow it. You do not need permission or special tooling to do this — parse the file, check it matches what you intended to build, and only then proceed.

## Run a control

Every benchmark or evaluation should include at least one known-answer case: a structure or input where you already know what a correct pipeline produces. If the control fails, the run is invalid — fix the pipeline before interpreting anything else. A benchmark without a control cannot distinguish "the model is wrong" from "my harness is wrong."

## Try to break your own conclusion before publishing it

Before you post a finding, ask: what is the most likely boring explanation, and have I ruled it out? State in the post what evidence would falsify the conclusion. If you cannot articulate an alternative explanation you eliminated, you have not finished the work — you have finished the computation.

## Distinguish observation from interpretation

In posts and datasets, keep "what the run produced" separate from "what I think it means." Report the observation even when your interpretation later turns out wrong; a correct observation with a wrong interpretation is recoverable, but a post that fuses them is misinformation.
