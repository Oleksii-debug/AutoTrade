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
  non-positive price and ambiguous time.

The seam never authenticates, opens a network session, creates brokerage
credentials, or submits an order. A constructed `MarketOrder` is only an
in-memory compatibility object.

This does not prove full WP-02, provider qualification, restart/reconciliation
correctness, packaging, performance, economic edge, or live trading safety.
The remaining WP-02 path still includes engine embedding, event
ordering/callback characterization, shutdown/restart behavior, guarded
brokerage isolation, and exact dependency/notice composition under WP-03.
