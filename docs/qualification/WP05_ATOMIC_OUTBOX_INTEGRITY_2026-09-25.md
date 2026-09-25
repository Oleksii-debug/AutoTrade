# WP-05 — atomic journal/outbox integrity repair — 2026-09-25

Status: **IMPLEMENTATION_INCREMENT_AWAITING_EXACT_HEAD_CI**

This record covers a bounded repair in the existing SQLite journal/outbox authority. It adds no network sender and no live trading authority.

## Exact lineage

- Base main: `92ca4e0cfd8949645663eeaf092ac2db156ec75a`.
- Implementation/test head before this evidence record: `31caea67f9ba699e8d801ac7295479e5aa6c7f27`.
- Canonical pull request: #548.
- Production path: `mvp/autotrade_mvp/persistence.py`.
- Focused test path: `mvp/tests/test_persistence.py`.
- Exact final PR-head evidence must come from repository CI after this document commit.

## Defect and repair

Schema v4 requires an `outbox.envelope_hash` integrity value and `pending_outbox()` verifies that value before exposing a publication intent. The single-event `append_event()` path already persisted the SHA-256 value. The atomic `commit_command()` path did not, so a transaction could durably commit command dedupe, financial events and an outbox row that subsequently failed its own integrity gate.

The repair computes SHA-256 over the canonical stored outbox envelope and writes the hash in the same SQLite transaction as command dedupe, journal events and outbox intent. Schema v5 also adds integrity binding for stored command results. During v4→v5 migration, a hashless outbox row is backfilled only when its entire legacy envelope is exactly reconstructable from the authoritative journal row; unknown/extra bytes are rejected rather than blessed with a new hash.

The same integrity identity now crosses the delivery boundary. `pending_outbox()` returns the verified immutable `envelope_hash`; `mark_outbox_delivered()` requires the caller to echo that exact hash, revalidates the stored envelope and authoritative journal event inside one `BEGIN IMMEDIATE` transaction, and uses the hash in the delivery compare-and-set. A stale process cannot acknowledge a different or tampered publication intent merely because its `outbox_id` is unchanged.

## Recovery/idempotency regression

The focused atomic-command test now:
1. commits command + ordered events + one publication intent;
2. closes that logical process boundary and constructs a fresh `JournalStore` over the same database;
3. reads and verifies the pending outbox after restart;
4. replays the same scoped idempotency key and confirms no duplicate event/outbox creation;
5. proves a pre-restart delivery receipt cannot mark a replaced envelope delivered;
6. proves delivery acknowledgement revalidates authoritative event integrity before setting `delivered_at`.

Existing tests continue to cover event/payload tamper rejection, aggregate-version gaps, transaction rollback, migration rollback, scoped command dedupe and legacy migration. A dedicated regression now proves that v5 migration rejects a hashless legacy outbox envelope containing an unverifiable extra field instead of minting a fresh integrity hash for it.

## Remaining WP-05/product work

This repair is not WP-05 DONE. The package still requires canonical EventEnvelope/UiCommand runtime-boundary integration, systematic crash-at-each-commit-boundary qualification, projection rebuild equivalence evidence, production placement/integration, Windows exact-head CI and release-level recovery qualification. No claim here substitutes for those gates.
