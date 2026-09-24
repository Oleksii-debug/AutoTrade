# WP-65 — Runtime resource budget foundation

Evidence date: 2026-09-24  
Source branch: `work/wp65-runtime-resource-budget-20260924`

## Scope proved by this change

This foundation adds a fail-closed evaluator for measured runtime-load evidence. It does not add another scheduler, admission authority, throttler, execution authority or trading policy.

A declared scenario binds:

- exact release commit SHA;
- SHA-256 configuration identity;
- privacy-preserving SHA-256 target-host fingerprint;
- strategy horizon;
- maximum p95 financial-processing latency;
- maximum financial-state staleness;
- maximum measured research interference;
- minimum evidence sample counts.

An observation records the expected and recovered financial-event counts, latency and staleness samples, research interference and remaining reconnect backlog. A scenario cannot pass if any financial event is lost, reconnect backlog remains, measured bounds are exceeded, or the evidence sample is too small.

The percentile implementation uses integer nearest-rank arithmetic. Performance evidence is never extrapolated from one scenario to another, another release commit, another configuration or another target host.

## Test evidence encoded in the repository

`mvp/tests/test_runtime_resource_budget.py` covers:

- deterministic nearest-rank p95;
- latency/staleness budgets bounded by the declared strategy horizon;
- event loss as a hard failure even when latency is low;
- incomplete reconnect backlog as a hard failure;
- slow-disk-like latency and stale-state failures;
- research/model interference above the declared budget;
- insufficient samples as `INCONCLUSIVE`, never `PASS`;
- scenario identity preventing evidence reuse across another workload;
- exact release/configuration/host binding preventing evidence reuse across another binary or target environment;
- a real `JournalStore` burst probe that persists and reconstructs every financial probe event and its outbox row.

The CI burst thresholds are intentionally generous. They prove that the measurement wiring works on the CI host and that financial events survive the probe. They do **not** qualify production throughput or latency.

## Still required for WP-65 completion

WP-65 remains incomplete until the final target hosts are measured with the declared production workload and burst margin. Qualification still needs representative:

- market-ingest and normalization load;
- guarded-dispatch and reconciliation activity;
- model/research contention;
- reconnect backlog recovery;
- degraded/slow disk behavior;
- long-running resource saturation and recovery;
- exact-build Windows measurements.

Those measurements must be tied to the exact release SHA, schema/configuration, host specification and strategy horizons. No universal throughput, low-latency or HFT claim is established by this foundation.
