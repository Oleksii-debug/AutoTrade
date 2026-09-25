# WP-16 — factor, execution-cost and derivative-risk hardening — 2026-09-25

Status: **IMPLEMENTATION_INCREMENT_AWAITING_EXACT_HEAD_CI**

This increment extends the existing independent risk authority. It does not create a second risk engine, does not send provider orders, and does not grant live-trading authority.

## Exact lineage

- Canonical main observed before the WP-16 stack: `1e4632906788d7b0f14567cc68bab8b53764946d`.
- Required predecessor stack: PR #211, exact observed head `9363b2103e50af8c713bae10f58a1590a397e736`.
- Implementation head before this evidence-only document: `8e12e73e2e10ae7f3a53bcb3878ff7ea23d1f866`.
- No shared JSON contract schema is changed by this increment.
- Exact final PR-head evidence must come from CI's `AUTOTRADE_SOURCE_SHA`; this document must not pretend to self-certify its own commit.

## Implemented risk boundaries

1. Correlated factor exposure: caller-supplied exact-Decimal factor loadings are aggregated over the complete projected marked portfolio, including reservations; missing configured evidence fails closed.
2. Execution quality: optional spread and slippage limits consume evidenced estimates and fail closed when configured evidence is missing.
3. Clock freshness: optional independent clock-age evidence prevents fresh market data from masking stale time synchronization.
4. Action policy: explicit action classes are policy-controlled; REDUCE/FLATTEN require reduce-only semantics and labels never bypass numerical limits.
5. Settlement: when required by policy, settlement must be affirmatively allowed; UNKNOWN is not permission.
6. Option exercise: EXERCISE is valid only for OPTION and can require verified deliverable plus exact exercise buying power.
7. Futures delivery: new FUTURE risk can require minimum delivery headroom; a genuine protective reduce-only close may proceed inside the cutoff.
8. Reproducibility: a deterministic SHA-256 fingerprint covers the complete ordered RiskDecision and its per-rule evidence.

All new financial values reject binary floating-point input and use Decimal-compatible inputs.

## Verification surface

`mvp/tests/test_risk.py` contains 52 focused risk tests at implementation head `8e12e73e2e10ae7f3a53bcb3878ff7ea23d1f866`.

Required exact-head gates are the existing repository workflows:

- `python tools/verify.py` on Ubuntu and Windows;
- `python tools/baseline.py check` on Ubuntu and Windows;
- exact-head evidence artifacts emitted by `tools/write_ci_evidence.py`.

Until those checks complete successfully for the final PR head, this increment remains **INCONCLUSIVE**, not PASS.

## Deliberate unresolved limits

WP-16 is not DONE. The following remain product-level blockers or integration dependencies:

- final risk admission must be bound inside the canonical writer transaction with accepted WP-08/WP-15 state, reservations and version checks;
- factor, venue, liquidity, spread, slippage, settlement and derivative evidence must be sourced from qualified instrument/market/provider state, not strategy or model text;
- option assignment/early-exercise feeds, adjusted deliverables and multi-leg interim risk still require full provider/lifecycle integration;
- futures first-notice, physical-delivery and provider-specific liquidation/margin-tier evidence remain incomplete;
- expected-shortfall/liquidation-headroom and richer correlation-regime stress qualification remain separate hardening work;
- no real provider/account qualification or real-money authority is established by these tests.

Economic edge remains unproven. Risk correctness and economic profitability are separate claims.
