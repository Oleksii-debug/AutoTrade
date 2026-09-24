# WP-02 pinned LEAN adoption gate foundation — 2026-09-24

Status: **INCONCLUSIVE adoption foundation; QuantConnect LEAN is not embedded by this change.**

## Exact upstream identity

The selected engine remains:

- repository: `QuantConnect/Lean`
- commit: `985ef30ad3ac774218c5ac516b4cb0aa2655730f`
- tree: `4b163abf9fca60e731b76510b9ae6721ffff7e6c`
- license: Apache-2.0
- inspected license blob: `6faed93d21a1a9551b11434e87c9000a393d73f2`

The same identity already exists in `provenance/components.json`.

## What this foundation does

`LeanAdoptionGate` refuses to call the selected engine accepted unless all of
these independently evidenced probes pass:

1. exact source/package composition and notices;
2. embedding into the AutoTrade runtime;
3. Windows build;
4. Linux build;
5. packaging;
6. authoritative decimal-boundary behavior;
7. deterministic event/callback ordering;
8. restart plus provider reconciliation;
9. provider-adapter isolation.

Any explicit failure produces `FAIL`. Any unresolved probe produces
`INCONCLUSIVE`. A pin mismatch is a failure. A passing adoption decision still
sets `LiveTradingAuthorityGranted=false`.

The console harness verifies only the gate logic and the .NET decimal primitive.
It is deliberately **not** evidence that LEAN itself passed these runtime probes.

## Authority boundary

This package does not create an OMS, journal, scheduler, risk engine, portfolio
truth, reconciliation authority or brokerage sender. AutoTrade's journal,
authority/risk gates, guarded execution and provider reconciliation remain
canonical. LEAN is intended to be a reusable derived financial-engine runtime
behind those boundaries.

## Why no LEAN source/package is imported yet

The current provenance record says
`source_import_allowed=PENDING_EXACT_COMPOSITION_AND_NOTICE_REVIEW`. WP-03 has
not yet completed the exact source/package composition, transitive graph and
release NOTICE review. Importing upstream bytes now would violate the approved
dependency policy.

`lean.adoption.evidence.json` therefore records every runtime probe as
`INCONCLUSIVE`. That file must not be changed to PASS from unit-test fixtures;
it requires evidence from the actual pinned composition and exact-head
qualification runs.

## Remaining WP-02 work

After WP-03 permits the exact composition, embed only the pinned LEAN surface,
run the clean Windows/Linux build and packaging probes, characterize decimal and
event-order semantics, perform restart/reconciliation and adapter-isolation
drills, and bind the resulting evidence to the exact AutoTrade and LEAN source
identities. Only then can WP-02 move beyond INCONCLUSIVE.
