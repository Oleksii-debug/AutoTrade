# WP-12 causal feeder foundation — 2026-09-24

Status: **implementation foundation only; WP-12 is not complete.**

This change implements a deterministic causal publication boundary under
`research/autotrade_research/evaluation/replay/`.

## Implemented invariants

- Every observation preserves separate event time, evidenced historical availability
  time, and ingest provenance time.
- Ingest time never substitutes for historical availability; delayed archive ingestion
  does not delay or advance the historical information cutoff.
- Availability cannot precede event time, and ingest provenance cannot precede the
  evidenced availability it claims to preserve.
- The event schema is explicitly versioned and its version participates in the
  immutable event digest.
- Strategy-facing views contain only events whose evidenced availability is at or
  before the simulation clock.
- Same-time ordering is deterministic by availability, source priority, source
  sequence and immutable event identity.
- The ordering-policy version, external manifest digest and all ordered event digests
  participate in the dataset identity.
- Direct construction of a dataset cannot forge its digest, hide duplicate event
  identities or supply a non-canonical order.
- Checkpoints bind exact manifest, exact dataset, causal published prefix, simulation
  time, cursor and a versioned strict record schema.
- Restore rejects a changed dataset, tampered prefix, cursor that consumed future
  data, cursor that skipped already-available data, malformed record, or unsupported
  checkpoint schema.
- The simulation clock cannot move backwards.
- Payloads are recursively immutable and reject binary floating-point values.

Focused local verification after hardening: **17/17 tests passed**. Additional
determinism checks exercised all permutations of the same-time fixture, every replay
publication boundary, and multiple Python hash seeds.

## Deliberate boundary

This is an **in-process causal API**, not hostile-code isolation. The canonical replay
architecture requires process/filesystem/network restrictions before untrusted
generated strategies can be described as isolated. No such claim is made here.

This foundation also does not claim completed LEAN replay integration, production
DatasetManifest loading, full market/history integration, ExperienceEpisode
checkpointing, RNG/strategy/account/journal checkpoint state, provider qualification,
release readiness, or economic-edge evidence. Those remain downstream WP-12
integration and qualification work.

No provider network call, live trading authority or real-money operation is
introduced.
