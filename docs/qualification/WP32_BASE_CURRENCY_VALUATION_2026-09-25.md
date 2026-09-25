# WP-32 exact base-currency valuation boundary

Date: 2026-09-25  
Starting base: `main@593e6cbba2970c4a0564aa76024c54b7c30ab5a9`

## Scope

This increment closes the dimensional-allocation seam identified in issue #639 without creating a second portfolio, accounting, FX or risk authority.

The canonical WP-32 allocator can now carry a `CandidateEconomicValuation` that binds:

- instrument version and payoff kind;
- quote, settlement and portfolio base currency;
- exact Decimal contract multiplier;
- exact Decimal FX-to-base rate and immutable FX evidence identity when conversion is required;
- valuation identity and validity window.

For valued candidates, all target notional, gross/net exposure, capital requirement, stress P&L and objective utility are computed in the one declared base currency. Linear futures include the contract multiplier before portfolio aggregation.

The evidence-bound wrapper accepts immutable `VALUATION` evidence, checks it against the candidate and current instrument version, requires explicit execution/financing/funding/borrow cost components to sum to the canonical cost rate, binds the capital snapshot to the same base currency, and includes valuation evidence plus base currency in the decision digest.

## Fail-closed boundary

This increment does not approximate nonlinear products. `INVERSE_FUTURE` and `OPTION` valuation kinds remain no-increase / rejected until their canonical nonlinear payoff evidence is wired into WP-32. Settlement-currency conversion distinct from quote-currency conversion also remains blocked rather than guessed.

Legacy unvalued single-currency allocation behavior is retained for compatibility. The stronger evidence-bound dimensional path is activated when candidate valuations are supplied.

## Focused regression evidence

`mvp/tests/test_allocation_valuation.py` covers:

- same-currency cash equity numerical compatibility;
- EUR→USD exact FX conversion;
- linear-future contract multiplier exposure/capital;
- mixed base-currency rejection;
- mandatory cross-currency FX identity;
- binary-float rejection;
- evidence-bound valuation coverage and freshness;
- digest sensitivity to FX/multiplier identity;
- nonlinear option/inverse fail-closed behavior.

No provider send, credentials, live trading authority or economic-edge claim is introduced.
