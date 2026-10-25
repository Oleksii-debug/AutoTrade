# WP-27 — Alpaca option lifecycle account/environment scope and delayed reconciliation

Date: 2026-09-26  
Reconverged base: `main@af01c247b46e384ca7c02ac2875a63d8357ef2cb`.

## Defects closed

The option lifecycle polling path already distinguished exercise, assignment and expiration and refused to infer lifecycle absence from the order stream. Two safety gaps remained:

1. parsed lifecycle observations were not bound to the Alpaca account or PAPER/LIVE environment from which they were polled;
2. PAPER can expose an economic position/balance change before the corresponding option lifecycle activity is published, so restart-safe reconciliation needs an explicit unresolved obligation rather than interpreting same-day activity silence as "no lifecycle event".

## Increment

`AlpacaOptionLifecycleObservation` now binds immutable `account_id`, `environment`, lifecycle type, symbol, effective/observed timestamps, provider activity id and source-payload SHA-256.

The same canonical JournalStore now persists an `alpaca_option_lifecycle_obligation` aggregate:

- `PROVISIONAL` records one already-observed economic change without inventing exercise/assignment/expiration subtype;
- restart replay preserves that unresolved obligation;
- a later polled activity can resolve only the exact account/environment/symbol/economic-change obligation;
- exact retries are idempotent;
- one provider activity id cannot resolve two economic changes in the same account/environment;
- a conflicting late activity fails closed and leaves the obligation unresolved.

`classify_option_lifecycle_evidence(...)` returns `INCONCLUSIVE` whenever provider activity is absent, regardless of order-stream quietness. Order-stream silence therefore never becomes proof of lifecycle absence.

## Safety boundary

The durable lifecycle obligation is reconciliation evidence only. It does **not** apply cash or position effects, does not create a second order/accounting state machine, does not grant live authority, and does not turn a payload digest into provider authentication. Downstream economic reconciliation must still bind trusted provider evidence and prove the exact economic delta before any financial state transition is accepted.

No provider write, option exercise request, live-trading permission or economic-edge claim is introduced.

## Regression acceptance

The branch includes regressions for:

- quiet order stream + no activity => `INCONCLUSIVE`;
- same-day PAPER economic change => durable `PROVISIONAL`;
- restart while provisional => obligation survives unresolved;
- next-day assignment activity => resolves the existing economic change idempotently;
- duplicate activity cannot resolve two economic changes;
- mismatched symbol activity cannot resolve the obligation.
