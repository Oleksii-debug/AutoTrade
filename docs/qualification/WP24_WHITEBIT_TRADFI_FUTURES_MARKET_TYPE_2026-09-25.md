# WP-24 — WhiteBIT TradFi futures market classification

Date: 2026-09-25  
Base: `main@7b8bd88aa09d32d3295b1f38465ec7fa7c73920b`.

## Finding

The WhiteBIT market metadata parser already recognized `tradfiFutures` as a provider market type, but the execution-side market-rule validator accepted only `FUTURES` for an AutoTrade `FUTURES` intent. That created an internal contradiction: metadata could be parsed successfully and then be rejected solely because its futures subtype was `TRADFIFUTURES`.

Current WhiteBIT documentation describes collateral order endpoints as the route for margin and futures orders, and the provider's current market schema includes `spot`, `futures`, and `tradfiFutures` market types. This change therefore keeps the existing collateral execution route and classifies both provider futures types as futures for the local market-rule check.

Official references inspected:
- https://docs.whitebit.com/concepts/order-types
- https://docs.whitebit.com/api-reference/overview

## Safety boundary

This does **not** advertise or authorize TradFi futures on an account. `prepare_order_request` still requires exact, unexpired, provider/account/environment/instrument capability evidence admitting the requested order type and permission scope. Region/account/product restrictions remain capability-discovery facts; they are not inferred from geography or from this market-type classification.

## Regression

The focused fixture proves that a `tradfiFutures` market can pass the same guarded FUTURES preparation path only when matching exact capability evidence is supplied, and that the request still uses the canonical collateral order endpoint.

No live credentials, network send, real order, or economic-edge claim is involved.
