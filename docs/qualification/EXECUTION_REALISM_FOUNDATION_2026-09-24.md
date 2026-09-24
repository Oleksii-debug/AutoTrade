# Execution-realism foundation — 2026-09-24

Status: **deterministic conservative foundation; not full WP-13 qualification**.

This evidence note covers `mvp/autotrade_mvp/execution_realism.py`. The
module is a pure simulation oracle. It is not an OMS, provider adapter or live
execution path and cannot send financial orders.

## Canonical rules implemented

The implementation follows the execution-simulation requirements in
`06_REPLAY_SCIENTIFIC_VALIDATION_ARCHITECTURE.md` and the economic constraints
in `04_PORTFOLIO_RISK_ECONOMIC_ARCHITECTURE.md`:

1. An order cannot fill from liquidity whose market timestamp is at or before
   its latency-adjusted venue arrival time. This prevents same-event and earlier
   liquidity from being reused after a decision.
2. `BAR`, `TOP_OF_BOOK` and `BOOK` fidelity are explicit. BAR mode reports
   that intrabar ordering/queue priority is unknown; top-of-book reports that
   queue priority is unknown.
3. BAR stop-limit execution fails conservatively when both trigger and limit
   are touched in the same candle and chronology cannot be proved. Trigger
   state can then carry into a later observation.
4. Fill quantity is bounded by observed volume, a registered participation
   ceiling and lot-size rounding. No partial is rounded upward.
5. Market fills use the adverse executable side of the observation and add
   registered slippage/impact assumptions. BAR mode may additionally include a
   conservative spread allowance.
6. Limit fills do not assume price improvement; the registered limit price is
   used when executable.
7. Fees and minimum charges are exact Decimal calculations. Binary float
   financial inputs are rejected.
8. Every model carries an immutable model version and calibration SHA-256.
   The complete assumption set has a deterministic fingerprint so experiments
   can freeze the exact cost/fill model.
9. BASE/ADVERSE/OPTIMISTIC scenarios are explicit. OPTIMISTIC results are
   marked as insufficient promotion evidence.
10. ADVERSE scenario multipliers cannot be below 1.

## Focused evidence

The test bank covers:
- impossible same/earlier-event fills;
- latency exclusion;
- partial fills and lot rounding;
- no-fill below one lot;
- conservative limit pricing;
- ambiguous same-bar stop-limit ordering;
- later-bar continuation after trigger;
- adverse BAR execution;
- scenario cost stress;
- minimum fees;
- binary-float rejection;
- frozen model fingerprint/calibration identity;
- fidelity input requirements.

## Remaining WP-13 qualification

This foundation does **not** claim the package complete. Remaining work includes:
- differential qualification against the pinned LEAN fill/fee/slippage/calendar
  interfaces once WP-02 exact runtime adoption is accepted;
- calibrated asset/provider-specific models for equities, spot, margin,
  futures, perpetuals and options;
- queue/depth modelling for qualified book data;
- rebates and venue-specific minimum/maximum fee schedules;
- funding, borrow/financing, FX and lifecycle cost integration without
  double-counting accounting postings;
- halt/gap/delisting/outage and reject/cancel-delay campaigns;
- calibration artifacts with lawful source provenance;
- exact-head cross-platform and whole-replay evidence.

Paper/testnet fills remain operational evidence only and are never treated as
proof of live profitability.
