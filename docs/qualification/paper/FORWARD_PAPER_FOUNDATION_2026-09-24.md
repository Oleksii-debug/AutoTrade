# WP-57 forward-paper evidence foundation — 2026-09-24

Status: **mechanism implemented; no provider campaign is qualified by this
change**.

## Purpose

This foundation makes it harder to accidentally convert backtest knowledge,
late observations or incomplete paper operations into a forward-evidence claim.
It does not schedule experiments, call a provider, submit an order, select a
strategy champion, or establish economic edge.

A campaign protocol is frozen to:

- exact source/build SHA;
- protocol hash;
- start and end timestamps;
- a protocol-declared minimum prediction count;
- maximum decision latency;
- the exact provider/capability surfaces advertised for the campaign;
- the operational cases that the protocol requires, such as reconnect and
  manual/external activity.

Each prediction records a causal information cutoff, sealing time, decision
deadline, future outcome horizon and immutable input/proposal hashes. Outcomes
cannot be treated as forward evidence if they were available before the
registered horizon. Binary floating-point costs are rejected.

## Assessment semantics

The evaluator keeps three different questions separate:

1. **Evidence validity** — `VALID`, `INCONCLUSIVE`, or `INVALID`.
2. **Operational result** — `PASS`, `FAIL`, or `INCONCLUSIVE`.
3. **Economic edge** — always `NOT_ESTABLISHED` in this foundation.

A missed decision deadline or unreconciled operational incident is valid
negative evidence, not something to discard. Missing outcomes, unfinished
campaign time, missing provider/capability coverage, incomplete costs, missing
required reconnect/manual-activity evidence, or incomplete account
reconciliation cannot become `VALID`.

There is deliberately no universal hard-coded campaign duration or trade count.
Those quantities belong to the frozen protocol and its statistical power /
coverage rationale.

## Repository evidence

- `research/autotrade_research/forward_paper.py`
- `research/tests/test_forward_paper.py`

The tests cover exact build/protocol binding, causal cutoffs, registered outcome
horizons, provider/capability coverage, decision deadlines, reconnect/manual
activity, exact costs, incomplete account reconciliation, duplicate outcomes and
the rule that a mechanically valid campaign still cannot declare economic edge.

## Still required for WP-57

WP-57 depends on the completed provider adapters, scientific gates, recovery,
whole-flow integration, product operability and runtime-resource qualification.
Those dependencies are not all accepted in current main.

Real forward campaigns must subsequently be run over time against the exact
paper/test facilities that each provider actually offers. They need frozen
predictions, actual costs and deadlines, reconnect/history-lag/manual-activity
evidence, full account reconciliation and a separately locked statistical
evaluation. If the available evidence is insufficient, the correct result is
`INCONCLUSIVE`, not an inferred edge claim.

## Product item 22 hardening — current branch

The Product Specification's paper-trading requirement now has stricter
source-level mechanics on this lineage. A mechanically complete campaign must
pre-register regime coverage, a minimum number of distinct decision-dependence
units per regime, a content-addressed evaluation profile, the reporting
currency, a maximum drawdown bound, and the simulator/test-environment
limitations that are expected to apply.

Each sealed prediction is assigned to a registered regime and dependence unit.
Those unit identifiers are de-duplication structure only: they do not by
themselves prove statistical independence. Independence, uncertainty,
stability, power/precision and promotion semantics remain the responsibility of
the separately frozen evaluation profile and scientific qualification gates.

Paper economics are recorded per decision with exact decimal gross P&L, fees,
spread cost, slippage cost, net P&L, realized time, equity-before/equity-after
and prior peak equity. The evaluator rejects inconsistent accounting identities,
missing or duplicate economics sequence steps, chronological regression,
economics recorded before the corresponding forward outcome is available,
balance/peak resets and per-decision execution costs that exceed the aggregate
reporting-currency cost ledger. Drawdown is evaluated on one continuous campaign
equity path; a registered-limit breach is retained as valid negative operational
evidence rather than discarded.

Unexpected simulator limitations make the campaign incomplete until the
qualification protocol is explicitly reconsidered. Legacy protocol hashes remain
readable, but a legacy campaign without these Product item 22 registrations is
INCONCLUSIVE.

This hardening still does **not** make a provider campaign qualified. Terminal
WP-57 acceptance additionally requires independently trusted immutable evidence,
accepted qualification trust, independent occurrence chronology and complete
provider/financial/reconciliation source-universe evidence. No result in this
foundation grants trading authority, and economic_edge_status remains
NOT_ESTABLISHED.

