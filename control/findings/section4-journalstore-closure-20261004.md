# Section 4 JournalStore closure candidate

Date: 2026-10-04
Parent hardening head: `d15b7be39b8938b7cdcd5ef9c70cf2c0d09a8fa3`

This branch promotes the current-main JournalStore crash/snapshot hardening into a Section 4 closure lane without creating a second persistence implementation.

## Existing authority

Current main already contains the canonical SQLite WAL journal/outbox/command-deduplication primitive. The parent hardening adds:
- one-SQLite-snapshot journal-tail/checkpoint validation;
- one canonical serialization cut for event/command/checkpoint payloads;
- crash-boundary regressions proving no partial event/command/outbox publication;
- restart-safe exact retry semantics;
- mutable-caller-state detachment before durable authority use.

## Section 4 closure requirements

The section can be declared DONE only if the current JournalStore as integrated product authority proves:
1. canonical physical store identity;
2. WAL and migration behavior;
3. monotonic sequence/CAS semantics;
4. command dedupe;
5. EVENT_BATCH/outbox atomicity;
6. crash-safe append and command commit;
7. restart/rebuild consistency;
8. concurrency safety at tested authority boundaries;
9. checkpoint/cut validation against the same durable snapshot;
10. compatibility with backup/restore consumers;
11. exact-head baseline and Verify terminal green;
12. merge and post-merge readback.

No provider/PAPER/LIVE, profitability, signed-release or NVDA qualification is implied.
