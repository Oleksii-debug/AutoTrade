# Section 13 closure — independent hard risk

## Canonical result

Section 13 / WP-16 provider-free independent hard-risk authority is closed on current `main`.

Canonical integrated lineage:
- Section-13 closure PR #1594, exact head `024cd913c85b48c41f8e94f7e2efa19c45e288ae`, merged as `461793df8702beba2996276afdca0dbf69709181`;
- stronger hard-risk trust-ingress successor #1726, exact head `3e8f17bfc6fffe00ab787cdf2fe1142438308b2c`, merged as `b847b6b97d8cc5970ac827854293f17b92b8e0c0`;
- current `risk.py` remains byte-identical to the accepted #1726 hard-risk result, while current authority, policy, valuation and FX layers are later stronger descendants.

## Hard-risk authority present on main

Current provider-free risk authority includes:
- exact Decimal/Fraction hard comparisons independent of ambient Decimal context;
- finite/resource-bounded scalar and intermediate arithmetic;
- position, single-notional, gross/net leverage and reserved-exposure limits;
- daily-loss and drawdown limits;
- market-data, FX and clock freshness;
- margin headroom and capability gates;
- affirmative borrow requirements for new short exposure;
- liquidity participation and asset/venue concentration;
- factor/correlated exposure aggregation;
- labeled stress-regime coverage and exact stress-loss limits;
- exact expected-shortfall tail selection and tail limits;
- liquidation-headroom authority from authenticated evidence;
- settlement evidence requirements;
- option exercise / buying-power / deliverable obligations;
- derivative-equivalent exposure and instrument-family consistency;
- futures delivery-headroom checks;
- strict reduce-only exception semantics that cannot increase risk;
- deterministic evidence-sensitive risk decision fingerprints.

Strategy/model output cannot override these hard rules.

## Durable quantitative policy authority

Current main carries the durable quantitative RiskPolicy authority:
- content-addressed immutable policy identity and version;
- durable registration/activation chronology;
- provider/account/runtime/provider-environment/entity-policy/instrument-family scope;
- rollback/downgrade prevention;
- historical policy replay at an exact journal cut;
- exact physical JournalStore generation binding;
- registry-issued `ResolvedRiskPolicy` use-time seal;
- caller mutation/subclass/method-shadow/rebinding defenses.

`RiskAuthorityRequest` and `AuthoritativeRiskSnapshot` carry the sealed resolved quantitative policy rather than accepting caller-owned financial policy state.

## Durable valuation / freshness authority

Current main also contains the provider-neutral valuation substrate:
- durable MARK / FX_QUOTE observation history;
- correction chronology revalidated on replay;
- exact historical journal-cut selection;
- physical JournalStore generation binding;
- registry-issued policy freshness authority;
- MARK freshness from `max_data_age_seconds`;
- FX freshness from `max_fx_age_seconds`;
- exact microsecond/rational boundary evaluation;
- deterministic valuation/freshness evidence identity;
- diagnostic observations cannot be promoted into production-origin evidence.

Positive PROVIDER_ORIGIN issuance remains deliberately fail-closed until the separately owned provider-origin / qualification authority is composed.

## Representative current-main regression evidence

Current test suites include:
- 100 tests in `mvp/tests/test_risk.py`;
- 33 tests in `mvp/tests/test_risk_exact_arithmetic.py`;
- 52 tests in `mvp/tests/test_risk_policy_authority.py`;
- policy downgrade, registry-binding and unforgeable-issuer regressions;
- authenticated liquidation-store authority regressions;
- 42 exact FX valuation regressions;
- 28 durable valuation-authority regressions;
- provider availability / capital and allocation binding regressions.

Representative falsifiers cover:
- stale state/data/FX/clock evidence;
- missing stress scenarios and stress labels;
- leverage/concentration/participation boundaries;
- exact 1/3 expected-shortfall and concentration comparisons;
- hostile Decimal/container/text/datetime/domain subclasses;
- oversized numeric-resource inputs and intermediates;
- liquidation evidence scope/account/environment/tamper mismatch;
- risk-policy rollback, store rebinding and direct construction forgery;
- valuation correction chronology, stale/future evidence, physical-store mismatch and self-declared provider-origin replay.

## Section boundary

This closure is the provider-free independent-risk authority of WP-16.

It does **not** claim that PAPER/LIVE provider-origin financial admission is enabled. The product intentionally continues to reject non-SIMULATION financial risk resolution unless a product-owned accepted provider account / qualification / instrument / provider-origin composition exists.

That downstream seam is tracked by #987 and #652 and belongs to provider/execution qualification. It must not be closed by accepting caller-authored RiskContext, valuation evidence or provider observations.

No provider qualification, real-money authorization, profitability/economic-edge, release or NVDA qualification follows from Section 13 closure.
