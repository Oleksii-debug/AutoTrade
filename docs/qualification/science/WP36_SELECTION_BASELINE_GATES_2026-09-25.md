# WP-36 selection, baseline and trial-control gates — 2026-09-25

Status: **scientific-evaluation increment; not a profitability claim and not trading authority**.

This extends the single existing `evaluation/gates.py` authority. The gate
profile must now pre-register decision controls that were previously absent:

- primary baseline and the exact comparison baseline set;
- selection/multiplicity correction identity;
- maximum attempted-trial budget;
- required regime coverage.

Evaluation evidence must bind the actual baseline comparisons, correction used,
attempted trial count and covered regimes to those registered controls.

Fail-closed outcomes:
- missing evidence => INCONCLUSIVE;
- changed correction, exceeded/zero trial budget, missing required regime,
  changed baseline set or a primary-baseline/net-advantage mismatch => FAIL;
- duplicate/empty registration dimensions are rejected before evaluation.

This implements the document-06 rule that benchmark selection, trial budget,
multiplicity treatment and regime coverage are fixed before locked evaluation.
It does not choose a statistical method, manufacture confidence intervals, or
turn software tests/backtests into evidence of economic edge.

Remaining WP-36 work includes dependence-aware estimator implementations,
block-choice sensitivity, exposure-matched baseline construction, precision
analysis, full locked-holdout accounting and exact integration with the
scientific registry and release qualification.
