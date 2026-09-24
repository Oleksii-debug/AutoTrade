# WP-30 adjusted option exercise and risk evidence — 2026-09-25

Status: **implementation increment; no trading authority and no economic-edge claim**.

This change strengthens the existing canonical option lifecycle module. It does
not add another ledger, risk engine, provider adapter or model authority.

## Financial invariants

- Physical adjusted options now require explicit
  `exercise_cash_per_contract`. The cash exchanged on exercise/assignment is
  no longer inferred as `strike × contract_multiplier`.
- Adjusted `deliverable[]` remains exact and independent of multiplier. This
  prevents a non-standard share/cash/right deliverable from silently being
  treated as a standard 100-share contract.
- CALL/PUT and long/short direction continue to determine signed asset/cash
  obligations; resource checks remain fail closed.
- Cash-settled options cannot carry physical deliverables or physical exercise
  cash.

## Model-dependent risk evidence

`OptionRiskEvidence` records Greeks only as versioned estimates, bound to:
- instrument identity;
- model id/version;
- exact source Git SHA;
- SHA-256 input digest and schema version;
- market timestamp, calculation timestamp and expiry;
- explicit stress scenario results.

Scenario stress is mandatory and independently exposes worst loss. A Greek
snapshot cannot substitute for stress, and stale/future/cross-instrument
evidence is rejected. Binary float remains forbidden at the financial boundary.

This implements the architecture rule that option Greeks are model-dependent
estimates with model/market timestamps and that scenario stress remains
necessary.

## Still incomplete

WP-30 is not DONE. Remaining work includes:
- provider-qualified assignment/exercise event feeds and idempotent late events;
- exercise/assignment fees and premium/basis policy integration;
- exercise-window/calendar semantics per exact instrument;
- volatility-surface provenance and independent model/oracle validation;
- multi-leg per-leg fills/corrections and interim protection integration;
- end-to-end crash/replay/reconciliation evidence on the accepted exact head.

No real provider request or real-money action is performed.
