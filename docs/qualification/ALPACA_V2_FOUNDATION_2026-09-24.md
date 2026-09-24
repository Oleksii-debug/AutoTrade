# Alpaca Trading API adapter foundation — 2026-09-24

Status: **implementation foundation only; NOT QUALIFIED for paper or live authority**.

The implementation in `mvp/autotrade_mvp/alpaca_v2.py` is deliberately
network-free. It owns no credentials, dispatcher, retry policy, journal,
reconciliation engine, capability registry or trading authority.

## Current official documentation used

- Create Order, Trading API v2:
  https://docs.alpaca.markets/us/reference/postorder
- Orders and supported TIF/order-type combinations:
  https://docs.alpaca.markets/us/docs/orders-at-alpaca
- Crypto orders:
  https://docs.alpaca.markets/us/docs/crypto-orders
- Options orders:
  https://docs.alpaca.markets/us/docs/options-orders
- 2026-08-27 options stop/stop-limit support:
  https://docs.alpaca.markets/us/v1.1/changelog/2026-08-27-options-stop-orders-08e9371
- 2026-08-28 options GTC support:
  https://docs.alpaca.markets/us/v1.1/changelog/2026-08-28-options-gtc-15a3de4
- Get order by client order ID:
  https://docs.alpaca.markets/us/reference/getorderbyclientorderid
- Account activities and pagination:
  https://docs.alpaca.markets/us/docs/account-activities
- Paper trading limitations:
  https://docs.alpaca.markets/us/v1.4.2/docs/paper-trading
- 2026-06-24 Trading API schema changes:
  https://docs.alpaca.markets/us/v1.1/changelog/2026-06-24-trading-api-00bf221

## Foundation semantics

1. Reuses the canonical `CapabilitySnapshot`; no Alpaca-specific capability
   authority is created.
2. Supports a bounded single-leg subset for equities, crypto and options.
   Capability evidence may narrow it further.
3. Exact Decimal quantity/prices are mandatory; binary floating-point money or
   quantity is rejected.
4. Option quantity must be whole contracts. Notional orders and multileg
   options are intentionally deferred rather than guessed.
5. Crypto TIF is bounded to GTC/IOC. Options are bounded to DAY/GTC.
6. Extended-hours requests are allowed only for equity LIMIT + DAY/GTC.
7. A successful POST /v2/orders Order object is only `ACKNOWLEDGED`.
   It never becomes a fill merely because the returned order status is filled.
8. Financial fills are derived from separately identified FILL activities.
   Because the documented TradeActivity shape does not provide canonical
   per-fill fee currency/amount, the adapter refuses to invent zero fees and
   requires fee evidence before constructing `ProviderFillEvidence`.
9. A single empty lookup/list never proves a possibly-sent order absent.
   Reconciliation surface evidence defaults fail-closed until exact provider
   qualification establishes pagination, consistency horizons and exclusion
   semantics.
10. Paper and live are separate evidence environments. Paper simulation does
    not prove live execution realism.

## Still required for WP-27 qualification

The exact adapter build must pass every case in
`provider_core.REQUIRED_QUALIFICATION_CASES`, including authentication,
clock behavior, quota/retry behavior, stream gap/reconnect, partial fill,
cancel/fill race, timeout-after-send ambiguity, duplicate/lost event,
corrections, account-mode changes, manual activity, full snapshot
reconciliation and secret redaction. Recorded paper evidence is not live
qualification, and live authority remains disabled until separate approval.
