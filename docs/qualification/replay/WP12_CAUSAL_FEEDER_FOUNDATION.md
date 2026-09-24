# WP-12 causal feeder foundation — 2026-09-24

Status: **implementation foundation only; WP-12 is not complete.**

This change implements the first deterministic causal publication boundary under
`research/autotrade_research/evaluation/replay/`.

## Implemented invariants

- Every observation has separate event time and evidenced availability time.
- An observation cannot become available before its event time.
- Strategy-facing views contain only events whose availability is at or before
  the current simulation clock.
- Same-time ordering is deterministic by availability, source priority, source
  sequence and immutable event identity.
- Dataset identity binds the external manifest digest and every ordered event
  digest.
- Checkpoints bind the exact manifest, exact frozen dataset, causal prefix,
  simulation time and cursor.
- Restore rejects a changed dataset, a tampered prefix, a cursor that consumed
  future data, or a cursor that skipped data already available at its clock.
- The simulation clock cannot move backwards.
- Payloads are recursively immutable and reject binary floating-point values.

Focused local tests executed before publication: 8/8 passed.

## Deliberate boundary

This is an **in-process causal API**, not hostile-code isolation. The canonical
replay architecture requires process/filesystem/network restrictions before
untrusted generated strategies can be described as isolated. No such claim is
made here.

This foundation also does not claim completed LEAN replay integration,
production DatasetManifest loading, full market/history integration,
ExperienceEpisode checkpointing, RNG/strategy/account/journal checkpoint state,
or economic-edge evidence. Those remain downstream WP-12 dependencies and
qualification work.

No provider network call, live trading authority or real-money operation is
introduced.
