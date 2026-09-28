# WP-12 causal feeder foundation — 2026-09-25

Status: **FOUNDATION_ONLY / NOT_FULLY_QUALIFIED**

This evidence is limited to the canonical WP-12 `REPLAY / causal-feeder`
responsibility. It does not grant trading, provider, release, or model authority.

## Exact implementation evidence

- canonical PR: #450
- reconverged base: `main@92ca4e0cfd8949645663eeaf092ac2db156ec75a`
- implementation evidence head: `628af69730bc8c96099f55ae56492257ebd1a2e3`
- `research/autotrade_research/evaluation/replay/feeder.py` blob:
  `c91d347372216e209a9491e41bbbdacfb38da7d1`
- `research/autotrade_research/evaluation/replay/__init__.py` blob:
  `ac4b61b3e37b590b0d3a93ab0e8053cb16530c06`
- `research/tests/test_causal_feeder.py` blob:
  `ea5a8785f5d9ed82cac80ae6f69c2f0a66c69dbf`

This document is an evidence-only update after the implementation head above; the
current PR head may therefore be newer while the cited implementation blobs remain exact.

## Implemented causal/replay invariants

- Every privileged source event preserves separate event time, evidenced historical
  availability time, and archive-ingest provenance time.
- Historical `ingested_at` is provenance, not a substitute for historical
  `available_at`; delayed archive ingestion does not move the historical causal cutoff.
- `available_at >= event_time` and `ingested_at >= available_at`.
- Strategy-facing `CausalObservation` deliberately omits archive `ingested_at`,
  manifest digest and dataset digest so future curation metadata is not leaked into the
  blinded strategy input.
- Strategy-facing views contain only observations available at or before the
  simulation clock and reject duplicate identities, future observations, and
  non-canonical ordering even when directly constructed.
- Privileged `CausalInputEvidence` separately commits simulation cutoff, exact
  manifest digest, exact dataset digest, and exact published-prefix digest.
- Resume at the same checkpoint reproduces both the strategy-visible view and the
  privileged input-evidence digest.
- Same-time ordering is deterministic by availability, source priority, source
  sequence and immutable event identity.
- The ordering-policy version, external manifest digest and every ordered event digest
  participate in the frozen dataset identity.
- Event payloads are recursively immutable and binary floating point is rejected.
- Direct dataset construction cannot forge its digest, hide duplicate event IDs, or
  supply a non-canonical event order.
- Checkpoints bind schema version, exact manifest, exact dataset, simulation time,
  cursor, and exact published-prefix digest.
- Restore rejects changed datasets, tampered prefixes, consumed-future cursors,
  skipped-already-available cursors, malformed checkpoint records, and unsupported
  checkpoint schema.
- The simulation clock cannot move backwards.
- Finalized bar observations cannot become visible before evidenced final availability.

## Exact-head GitHub evidence

At implementation head `628af69730bc8c96099f55ae56492257ebd1a2e3`:

### Focused research-primitives workflow

Run `36104528541`:

- Ubuntu / Python 3.12: **SUCCESS**
- Windows / Python 3.12: **SUCCESS**
- workflow conclusion: **SUCCESS**

The Ubuntu job executed 205 research-primitives tests and reported `OK`.
The WP-12 module contained 20 focused causal-feeder tests in that run, including:
future availability, stable same-time ordering, checkpoint/resume, bar timing,
payload immutability, direct-constructor integrity, every publication-boundary resume,
future-view rejection, duplicate/reordered-view rejection, archive-ingest isolation,
content-addressed privileged input evidence, dataset identity, and backwards-clock
rejection.

### Baseline

Run `36104528626`: **SUCCESS**.

### Whole-product Verify

Run `36104528552`: **FAILURE** because the repository-wide contract phase still
contains the known Bybit/Kraken provider-contract incompatibilities already present on
canonical main. The focused WP-12 workflow is independently green; this document does
not relabel the whole-product Verify as passing.

## Deliberate remaining boundaries

This remains an **in-process feeder foundation**, not hostile-code isolation.
The canonical replay architecture requires process/filesystem/network restrictions for
untrusted generated strategies. No such claim is made here.

This foundation still does not establish:

- filesystem/process/network future-file isolation;
- production DatasetManifest loading and rights verification;
- accepted WP-02/LEAN replay integration;
- complete replay checkpoints containing RNG state, strategy/model state, positions,
  orders, pending venue events, journal digest, and all model/data/config versions;
- ledger/decision equality after restart end-to-end;
- latency-aware venue processing or impossible-fill prevention;
- causal universe/feature fitting guarantees outside the feeder boundary;
- execution-simulator fidelity qualification;
- full scientific protocol/holdout/forward-paper qualification;
- economic edge.

WP-12 must therefore remain NOT_FULLY_QUALIFIED until those integration and
qualification requirements have exact-head evidence.
