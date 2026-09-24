# WP-59 recovery/release qualification foundation — 2026-09-24

Status: **mechanism foundation only; real recovery qualification remains incomplete.**

## Purpose

This change adds an independent, read-only qualification gate for release recovery.
It does not implement a second recovery controller, journal, backup system, authority
manager, sender or provider adapter.

The gate consumes evidence produced by the canonical runtime/recovery components and
requires all evidence to bind to one exact AutoTrade source SHA and one exact release
artifact SHA-256.

## Required fault scenarios

A qualification set is incomplete unless it contains all of:

- power loss;
- network loss;
- storage loss;
- session loss;
- split-brain attempt;
- upgrade failure.

For every scenario the gate requires explicit evidence for journal integrity, backup
integrity, completed reconciliation, reacquired authority, fencing of the old sender,
zero observed data-loss events, zero duplicate external actions, zero unresolved
UNKNOWN submissions, zero unresolved reconciliation items, and a declared measured
downtime inside the scenario-specific limit.

Upgrade failure additionally requires completed rollback.  If open financial risk is
present during a scenario, the evidence must show either provider-native protection or
a separately qualified emergency path.

## Fail-closed semantics

- missing or explicitly inconclusive scenario evidence => INCONCLUSIVE;
- wrong source SHA or release artifact hash => FAIL;
- observed data loss, duplicate external action, unresolved UNKNOWN, reconciliation
  gap, integrity gap, missing authority reacquisition, stale sender, failed rollback or
  exceeded downtime limit => FAIL;
- PASS never grants trading authority.

This is deliberately stricter than merely showing that a process restarted.

## Remaining work

WP-59 is not complete until accepted WP-48/WP-49/WP-50/WP-57 artifacts are integrated,
the delivered Windows bundle is exercised on real qualification hosts, the required
fault drills are run against the exact release candidate, measured downtime and
limitations are captured, and the resulting evidence is consumed by the canonical
release qualification gate.

No external broker/funds recovery guarantee is claimed.
