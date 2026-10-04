# Binance USD-M Futures adapter foundation — 2026-09-25

Status: **implementation foundation only; NOT QUALIFIED for live, testnet or real-money authority**.

This change extends the existing Binance WP-25 lineage instead of creating a
second provider framework. It is deliberately network-free: no credentials,
signing, sockets or provider writes exist in this module.

## Retained provider semantics

The implementation was checked against current official Binance USD-M Futures
REST documentation on 2026-09-25.

- New order uses `POST /fapi/v1/order`.
- The foundation supports only MARKET and LIMIT orders. Financial quantity and
  price remain exact decimal text; binary float inputs fail closed.
- `newOrderRespType=ACK` is explicit. Placement acknowledgement is never
  treated as an execution fill, even if a recorded response carries status or
  executed-quantity fields.
- One-way mode is bound to `positionSide=BOTH`. Hedge mode requires LONG or
  SHORT, and `reduceOnly` is rejected in Hedge mode.
- Provider trade identity comes from USD-M account trade rows. Unique trade ID,
  exact quantity/price/commission, order identity and instrument-version mapping
  are preserved into the existing reconciliation evidence type.
- Empty order history is not absence proof. Binance documents retention windows
  that omit some canceled/expired unfilled orders after three days and orders
  older than ninety days, so absence remains fail-closed unless exact coverage
  and qualified exclusion semantics are independently evidenced.
- UNKNOWN/no-blind-retry remains owned by the existing provider/execution
  classifier and reconciliation authority; this adapter does not duplicate it.

Official reference:
- https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade

## Deliberately absent

- HTTP/WebSocket transport, API keys, signing, clock sync or request retries;
- live/testnet traffic and account entitlement qualification;
- conditional orders, GTD/RPI/priceMatch/STP or close-position semantics;
- exchange filter ingestion and contract-size/tick/step validation;
- account, margin, liquidation and position snapshot parsing;
- funding-income and realized-PnL accounting authority;
- COIN-M, Margin and Options implementations;
- any real-money trading authority.

WP-25 remains incomplete until exact-head provider qualification covers the
canonical cases in `provider_core.REQUIRED_QUALIFICATION_CASES`, including
authentication, clock, quota, stream-gap recovery, partial fills, cancel/fill
race, timeout-after-send, duplicate/lost events, corrections, account-mode
changes, manual activity, snapshot reconciliation and secret redaction.
