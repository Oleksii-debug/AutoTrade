# WP-02 LEAN adoption foundation

Status: implementation foundation only; WP-02 is not complete.

The approved engine source is QuantConnect LEAN commit
`985ef30ad3ac774218c5ac516b4cb0aa2655730f`, tree
`4b163abf9fca60e731b76510b9ae6721ffff7e6c`. The repository provenance
record remains the authority for license/composition status.

This slice adds a non-sending integration seam and a Linux/Windows
characterization workflow. CI checks out the exact approved LEAN commit,
verifies its Git identity before build, compiles through LEAN's actual
`Common/QuantConnect.csproj`, and runs an executable probe through the
AutoTrade boundary.

The probe checks:

- canonical AutoTrade Decimal input before it enters LEAN;
- exact positive/negative decimal quantity round-trip;
- exact decimal price round-trip;
- explicit UTC time;
- deterministic equity symbol construction;
- rejection of exponent notation, non-canonical decimal text, zero quantity,
  non-positive price and ambiguous time;
- acknowledgement versus economic-fill distinction, partial/final fill visibility,
  duplicate callback identity, conflicting repeated identity and arrival-time regression;
- clean-process restart of diagnostic callback identity state, proving that an identical
  callback remains duplicate, conflicting economics remain visible, and the prior
  arrival-time boundary survives restart.

The seam never authenticates, opens a network session, creates brokerage
credentials, or submits an order. A constructed `MarketOrder` is only an
in-memory compatibility object.

This does not prove full WP-02, provider qualification, restart/reconciliation
correctness, packaging, performance, economic edge, or live trading safety.
The restart probe is deliberately diagnostic state only: it is not the AutoTrade
journal, order projection, account truth or reconciliation authority. Full shutdown
and restart reconciliation still requires the canonical persistence/reconciliation
spine and provider evidence.

The remaining WP-02 path still includes fuller engine embedding, shutdown/restart
reconciliation, packaging, guarded brokerage isolation, and exact dependency/notice
composition under WP-03.
