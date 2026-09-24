# Alpaca multi-leg options foundation — 2026-09-25

Status: **implementation foundation only; NOT QUALIFIED for paper or live authority**.

This increment extends the canonical `mvp/autotrade_mvp/alpaca.py` adapter.
It does not create another provider, execution, capability, ledger or
reconciliation authority. No network call, credential or signing path is added.

## Current official semantics retained

Official Alpaca documentation checked on 2026-09-25 documents multi-leg option
orders through `POST /v2/orders` with `order_class=mleg`.

- the parent omits a single symbol/side and carries strategy quantity;
- the foundation supports only MARKET and LIMIT;
- each leg carries symbol, positive whole ratio quantity, BUY/SELL and required
  position intent;
- only 2–4 option legs are admitted in this foundation;
- all legs are bound to one canonical underlying and one account/environment;
- every leg needs its own exact `CapabilitySnapshot`; no symbol fallback;
- ratio quantities must be in simplest form (GCD = 1);
- LIMIT net price stays exact Decimal and preserves Alpaca debit/credit sign:
  positive is debit, negative is credit;
- binary float is rejected at the financial boundary;
- DAY is deliberately the only admitted MLeg TIF in this increment. Alpaca's
  current API reference and recent options GTC documentation are not silently
  generalized to MLeg until exact provider qualification proves that surface.

References:
- https://docs.alpaca.markets/us/docs/options-level-3-trading
- https://docs.alpaca.markets/us/reference/postorder
- https://docs.alpaca.markets/us/v1.1/changelog/2026-08-28-options-gtc-15a3de4

## Deliberately absent

- equity legs / covered-stock combinations;
- provider network, authentication, rate-limit or retry implementation;
- multi-leg fill decomposition and per-leg activity/fee reconciliation;
- cancel/replace race qualification;
- exercise/assignment, expiry and deliverable accounting;
- provider margin estimation as financial authority;
- paper/live execution-realism evidence;
- any real-money trading authority.

WP-27 remains incomplete until the exact adapter build passes all provider
qualification cases and the option lifecycle/risk/accounting authorities can
consume the resulting leg-level economic evidence without inventing fills,
fees, margin or assignment outcomes.
