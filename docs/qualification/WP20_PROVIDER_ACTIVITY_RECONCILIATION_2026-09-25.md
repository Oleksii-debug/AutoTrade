# WP-20 — provider activity and manual-account reconciliation — 2026-09-25

Status: **IMPLEMENTATION_INCREMENT_AWAITING_EXACT_HEAD_CI**

This increment extends the canonical `mvp/autotrade_mvp/reconciliation.py` account-truth authority. It does not create a second reconciliation service, journal, order projection, or execution authority.

## Exact lineage

- Source main at branch creation: `6fec062594f767e00039c127014c44f058387534`.
- Initial activity-reconciliation implementation: `75cda86f0bc3b346faf864cc8396e9d5bedb6e46`.
- Durable activity-gap checkpoint/restart increment: `45cae88877fdcafc8cc6d8e066207f9c31efa6b7`.
- The focused activity logic is covered by the reconciliation and reconciliation-journal regression surfaces.
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
- generic activity evidence never converts an ambiguous send into a fill, working order, or `PROVEN_ABSENT`;\n- working-order evidence without a per-order observation timestamp resolves an UNKNOWN send only when the coherent provider snapshot itself begins at or after that submission; a pre-submission snapshot cannot be used as causal send evidence;
- reconciliation checkpoints persist matched/unexpected/missing/manual activity identities and activity-coverage status;
- after journal restart, unresolved unexpected or missing provider activity identities can be recovered without creating resend authority.

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
- durable posting/import of acknowledged activity into accounting still needs same-transaction idempotency; checkpoint persistence now preserves unresolved activity gaps across restart but does not itself post economics;
- provider-specific evidence windows and consistency horizons to be qualified rather than assumed;
- whole-flow recovery and release qualification on delivered Windows artifacts.

This change grants no live trading authority and is not evidence of economic edge.


## Atomic external cash activity accounting increment

This lineage now also connects confirmed provider-normalized external cash activity to the existing canonical accounting book without creating another ledger authority.

Implemented on the same branch:
- only MANUAL/EXTERNAL `DEPOSIT` and `WITHDRAWAL` activity can be mapped to external equity cash flow;
- provider/account/activity identity is durable and account-scoped;
- provider evidence and `EconomicTransactionBooked` are committed in one `JournalStore.commit_command` transaction;
- exact retry after restart is a no-op;
- changed amount under the same immutable activity identity fails closed through command idempotency;
- amounts reject binary float and preserve exact Decimal text;
- withdrawal/deposit sign semantics are explicit;
- observed evidence cannot predate provider occurrence;
- restart rebuilds the existing `EconomicBook` from durable events;
- unsupported durable economic event types fail closed.

Deliberately not mapped here: generic cash adjustments, fees, rebates, dividends, funding, interest, corporate actions, assignments, corrections or UNKNOWN-origin activity. Those require qualified provider-specific normalization and their own canonical accounting mappings; until then they remain reconciliation blockers rather than being guessed into `EXTERNAL_EQUITY`.

Exact-head verification is required before merge. This increment still does not complete WP-20: full provider activity taxonomies, real provider/account fixtures, atomic import of all qualified lifecycle economics, statement-level reconciliation and crash qualification across the complete live recovery flow remain open.


## Provider/account-scoped activity evidence

Normalized provider activity evidence now carries the exact provider and account identity. Durable economic import verifies both identities before creating either the ProviderActivityImported event or its economic booking. The same activity object cannot be replayed under a different account or provider; identical provider activity IDs on different accounts require independently scoped evidence objects. This closes a cross-account attribution ambiguity at the external cash-flow boundary.


## Reconciliation scope binding

When provider activity evidence is supplied to account reconciliation, the caller must now provide the exact provider/account scope and every activity must match it. Activity-free reconciliation remains API-compatible. Cross-provider or cross-account activity cannot be used to explain, block, or complete another account's reconciliation result.
