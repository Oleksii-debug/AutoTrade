# Durable reservations foundation — 2026-09-24

Status: **persistent/restart-safe WP-15 foundation; not full atomic accounting integration**.

`mvp/autotrade_mvp/durable_reservations.py` binds the existing
`ReservationBook` to the canonical SQLite `JournalStore`. It does not
introduce another store or another financial authority.

Implemented invariants:

- journal-first mutation: the candidate reservation state is calculated on an
  isolated projection, committed through `JournalStore.commit_command`, then
  the live projection is rebuilt from durable history;
- crash after the journal commit but before process projection is recoverable
  by replay;
- restart never makes active or UNKNOWN reservations available again;
- aggregate versions provide optimistic concurrency fencing; a stale racing
  writer cannot silently append the same state version;
- command/idempotency identity is persisted with each reservation event;
- duplicate idempotent consumption is not applied twice;
- the same idempotency key with different economics fails closed;
- each replay verifies the journal payload hash and independently recomputes
  the reservation transition; even a tampered snapshot with a recomputed
  payload hash is rejected when it disagrees with deterministic replay;
- exact Decimal resource amounts are retained and binary floats remain
  forbidden;
- existing UNKNOWN/terminal semantics remain authoritative, including the rule
  that PROVEN_ABSENT/REJECTED cannot erase consumed exposure.

Focused tests exercise restart, partial consumption, UNKNOWN retention,
terminal recovery, double-spend prevention after restart, idempotent replay,
conflicting idempotency, simulated journal failure, two forms of journal
tampering, consumed UNKNOWN exposure and float rejection.

Remaining WP-15 work before qualification:

- bind reservation commit in the same financial transaction as the final
  accounting/risk admission version rather than only the journal command;
- derive and consume exact provider fill quantity, fee, margin and slippage
  resources end-to-end;
- prove multi-process contention under sustained concurrency, not only the
  aggregate-version fence;
- add migration/backward-compatibility evidence for persisted reservation
  events;
- integrate asset-specific collateral, settlement, options obligation and
  futures/perpetual margin resources;
- complete exact-head cross-platform recovery qualification.

No live provider calls or real-money authority are introduced.
