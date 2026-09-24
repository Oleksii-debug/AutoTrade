# WP-56 scientific-learning qualification

This branch adds an independent qualification layer for the scientific-learning package. It consumes evidence produced by the existing protocol, leakage, holdout, retention, promotion, ablation, uncertainty and forward-evidence paths. It does not replace those paths and it does not grant trading or release authority.

## Result semantics

The evaluator has exactly three scientific outcomes:

- `PASS`: every required evidence gate passes, holdout data was not reused for tuning, routing did not use future information, and the declared economic claim is no stronger than the evidence.
- `FAIL`: a gate failed, holdout misuse or future leakage was observed, or the economic claim exceeds its forward evidence.
- `INCONCLUSIVE`: no hard failure is known, but at least one required evidence gate is missing or inconclusive.

A visually strong backtest is intentionally not an input to this qualification layer. Exact SHA-256 identities are required for the candidate, frozen protocol, input snapshot and every gate evidence bundle.

## Safety boundary

A scientific `PASS` is evidence only. `release_or_trading_authority` remains false. Promotion, risk admission, dispatch and release qualification retain their own authorities.

## Local verification before publication

The implementation was exercised together with the existing science tests. The focused command was:

`python -m pytest -q tests/Science/test_science.py tests/Science/test_science_qualification.py`

Result in the local candidate environment: 16 passed. Exact-head CI on this GitHub branch remains required before integration.
