# Permanent-magnet screening

For a deterministic laboratory shortlist from the RE-free candidate dataset, call `run_coil("rank-re-free-magnets", {...})` rather than manually sorting the source table. The coil requires no arguments for its default, laboratory-ready ranking; pass `weights` (`magnetic`, `synthesizability`, `hhi`, `toxicity`) and optional threshold parameters only when an alternate, documented prioritization is intended. It creates or overwrites a config-fingerprinted output dataset and preserves CIF references plus raw and normalized metrics.
