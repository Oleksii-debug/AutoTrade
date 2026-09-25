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
