# WP-20 — provider activity and manual-account reconciliation — 2026-09-25

Status: **IMPLEMENTATION_INCREMENT_AWAITING_EXACT_HEAD_CI**

This increment extends the canonical `mvp/autotrade_mvp/reconciliation.py` account-truth authority. It does not create a second reconciliation service, journal, order projection, or execution authority.

## Exact lineage

- Source main at branch creation: `6fec062594f767e00039c127014c44f058387534`.
- Implementation commit before this evidence-only document: `75cda86f0bc3b346faf864cc8396e9d5bedb6e46`.
- Focused reconciliation test surface at that implementation head: 39 tests.
- Final PASS must be bound to the final PR head by existing Ubuntu/Windows verification workflows; this document cannot self-certify its own commit.

## Implemented boundary

The existing account reconciliation now also supports evidence-bound provider activity identity:

- provider activities carry a stable activity id, type, time, origin and optional instrument/currency/order/execution references;
- origin is explicit: `AUTOTRADE`, `MANUAL`, `EXTERNAL`, or `UNKNOWN`;
- conflicting observations for one provider activity id fail closed;
- local/provider activity identities are reconciled separately from fills and working orders;
- unexpected activities block the affected instrument and/or currency, or the whole account when no narrower scope is evidenced;
- manual, external and unknown-origin activities are surfaced for explicit import/reconciliation rather than silently discarded;
- expected local activity missing from provider evidence blocks account truth;
- when configured, the complete reconciliation window requires a dedicated complete `ACTIVITIES` coverage surface with elapsed consistency horizon;
- generic activity evidence never converts an ambiguous send into a fill, working order, or `PROVEN_ABSENT`.

## Required exact-head verification

Existing repository gates must pass on the final PR head:

- `python tools/verify.py` on Ubuntu and Windows;
- `python tools/baseline.py check` on Ubuntu and Windows;
- exact-head evidence emitted by `tools/write_ci_evidence.py`.

Until those runs complete successfully, this increment remains **INCONCLUSIVE**, not PASS.

## Deliberate unresolved limits

WP-20 is not DONE. Product completion still requires:

- provider adapters to normalize real account/activity taxonomies and pagination semantics into this evidence shape;
- durable import/posting of legitimate manual/external cash, position, fee, corporate-action and lifecycle events into canonical accounting/order truth;
- final binding to accepted order projection, execution, journal and provider capability lineages;
- restart/crash qualification proving that acknowledged activity import cannot be lost or double-applied;
- provider-specific evidence windows and consistency horizons to be qualified rather than assumed;
- whole-flow recovery and release qualification on delivered Windows artifacts.

This change grants no live trading authority and is not evidence of economic edge.
