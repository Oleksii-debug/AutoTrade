# WP-16 — concentration and liquidity hardening — 2026-09-25

Base source: `1e4632906788d7b0f14567cc68bab8b53764946d`.

This increment extends the existing independent risk authority; it does not create a second risk engine and does not grant live-trading authority.

Implemented, when explicitly configured by policy:
- projected gross concentration by caller-supplied asset bucket;
- projected gross concentration by caller-supplied venue;
- order participation against evidenced per-instrument liquidity capacity;
- fail-closed rejection when configured concentration identity or liquidity evidence is missing;
- exact Decimal-only limits and observations; binary floats remain rejected.

Concentration is measured as the largest bucket or venue share of projected gross marked notional. Liquidity participation is order quantity divided by evidenced capacity. Both are admission constraints, not estimates of economic edge.

Deliberate boundary:
- existing callers that do not configure these optional limits keep their previous behaviour;
- the finished product must source bucket, venue and liquidity evidence from qualified instrument/market/provider state rather than user/model text;
- correlation/factor limits, spread/slippage bounds, settlement restrictions and derivative-specific obligations remain further WP-16 integration work;
- no provider network calls, credentials, withdrawals or real-money sends are enabled by this change.

Additional fail-closed authority boundary:
- `RiskContext.create` no longer assumes permission, margin headroom, or borrow availability when evidence is omitted;
- callers must explicitly supply `capability_allowed`, `margin_headroom`, and `borrow_available`;
- `borrow_available=None` remains an explicit UNKNOWN state and blocks a new/increased short rather than being converted to permission.
