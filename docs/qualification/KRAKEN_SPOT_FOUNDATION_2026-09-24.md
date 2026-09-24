# Kraken Spot adapter foundation evidence — 2026-09-24

Status: **implementation foundation only; NOT QUALIFIED for live trading, Spot
sandbox/paper authority, or Kraken Derivatives authority**.

This evidence accompanies `mvp/autotrade_mvp/kraken_spot.py`. The module is a
pure translation/recorded-response layer. It performs no HTTP request, stores no
API key, signs no request and grants no financial authority.

## Semantics enforced by this foundation

- Spot and Kraken Derivatives remain separate API families.
- An AddOrder response is acknowledgement only and never fill evidence.
- The logical request contains no nonce/signature/deadline before AutoTrade's
  final guarded send barrier.
- Binary floating-point economics are rejected.
- `cl_ord_id` is validated against the bounded Spot contract.
- Recorded `TradesHistory` trade IDs become canonical
  `ProviderFillEvidence.provider_execution_id` values.
- Instrument version, client identity and fee currency are supplied from
  separately evidenced metadata/order state rather than guessed from pair text.
- Reconciliation coverage defaults to
  `provider_semantics_exclude_execution=false`; an empty endpoint is not
  automatically proof that an uncertain send did not execute.
- No withdrawal/transfer method and no real-money authority exist in this
  adapter foundation.

Kraken documents Spot REST AddOrder with `cl_ord_id`, and its account trade
history is retrieved from `TradesHistory` in pages of up to 50 records. The
adapter therefore treats trade-history rows as execution evidence, while order
acknowledgement remains a different fact.

## Current references

- https://www.kraken.com/features/trading-api
- https://support.kraken.com/articles/360000912103-how-to-retrieve-your-account-s-trading-history
- https://support.kraken.com/articles/206548387-where-can-i-find-documentation-for-the-api-

## Still required for WP-23

The package is not complete until exact-build provider qualification covers:
authentication/signature and durable nonce allocation; current asset/pair
metadata; account capabilities; quotas; open/closed/query-order coverage;
TradesHistory pagination and history lag; websocket execution gaps/reconnect;
partial fills; cancel/fill races; explicit rejection; timeout after possible
send; corrections; manual/external activity; account snapshot reconciliation;
credential rotation; and secret-redacted logs.

Kraken Futures/Derivatives remains a separately qualified adapter lineage. No
result here proves profitability, paper/live parity, or permission for
real-money trading.
