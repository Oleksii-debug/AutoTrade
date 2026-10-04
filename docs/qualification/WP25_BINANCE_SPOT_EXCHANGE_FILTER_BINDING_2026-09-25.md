# WP-25 — Binance Spot exchangeInfo filter binding

Date: 2026-09-25  
Base: `main@7b8bd88aa09d32d3295b1f38465ec7fa7c73920b`.

## Defect

The Binance Spot foundation required exact capability evidence before order preparation, but the economic request itself was not checked against the symbol's current `exchangeInfo` filters. A quantity or price violating LOT_SIZE/PRICE_FILTER, or a notional violating MIN_NOTIONAL/NOTIONAL, could therefore reach the guarded transport and be rejected only by the provider. The exact filter version used for admission also was not retained on the prepared request.

## Increment

`BinanceSpotSymbolRules` now parses and integrity-identifies one symbol's exact `exchangeInfo` filter set. The Spot preparation boundary requires these rules and fails closed on:

- instrument or provider-symbol mismatch;
- LOT_SIZE / MARKET_LOT_SIZE bounds and step violations;
- LIMIT PRICE_FILTER bounds and tick violations;
- MIN_NOTIONAL / NOTIONAL lower and upper bounds;
- MARKET notional filters when no causal reference price is supplied;
- malformed duplicate/missing filters and non-boolean market-application flags.

The prepared request retains `filter_source_sha256`, binding the provider request to the exact rules snapshot used for validation.

## Safety

This is pre-send validation only. It does not sign or send requests, infer account permissions, or retry provider writes. Capability evidence remains independently mandatory. For MARKET orders, a reference price is used only to prove that provider notional filters can be satisfied at preparation time; it is not a fill-price promise or profitability claim.

## Focused regressions

Tests cover exact LIMIT preparation, MARKET preparation with reference price, step-size rejection, market notional fail-closed behavior, exact instrument-version binding, strict boolean parsing, and filter-provenance retention.

USD-M filter integration remains a separate WP-25 increment.
