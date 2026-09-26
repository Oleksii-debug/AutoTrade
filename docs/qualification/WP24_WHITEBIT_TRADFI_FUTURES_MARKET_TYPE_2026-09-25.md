# WP-24 — WhiteBIT TradFi Futures forward-compatible classification

Date: 2026-09-26  
Lineage: `wp24/whitebit-tradfi-futures-market-type-20260925-sol`.

## Provider contract

The canonical market parser accepts the documented future
`type=tradfiFutures` enum only when the provider record also contains the
required `isTradFiFutures=true` flag. Missing, non-boolean, or inconsistent
type/flag combinations fail closed.

Classification and execution authority are intentionally separate. Current
WhiteBIT documentation describes TradFi Futures as coming soon rather than a
currently returned/executable public-market product. AutoTrade may therefore
parse a complete future provider shape as `TRADFIFUTURES`, but
`prepare_order_request()` rejects that subtype before a collateral order
request can be produced. A generic `ORDER_WRITE` capability fixture cannot
manufacture current product availability or route qualification.

## Regression

The focused tests now prove:
- ordinary SPOT/FUTURES fixtures include the required provider flag;
- a complete `tradfiFutures + isTradFiFutures=true` record is classified as
  the forward-compatible futures subtype;
- missing/false/inconsistent `isTradFiFutures` is rejected;
- even a synthetically matching generic capability cannot turn the
  not-currently-qualified TradFi subtype into an executable collateral request.

This does not authenticate WhiteBIT, submit a live order, infer regional
availability, or claim provider/product qualification.
