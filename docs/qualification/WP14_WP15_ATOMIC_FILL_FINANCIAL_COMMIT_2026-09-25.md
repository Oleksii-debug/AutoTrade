# WP-14 / WP-15 — atomic fill financial commit qualification boundary

Date: 2026-09-25.

This increment closes one specific durability gap between the canonical reservation authority and canonical economic ledger. It does not declare WP-14, WP-15, provider qualification, economic edge, release readiness, or real-money trading authority complete.

## Problem closed

Before this increment the whole-simulator flow could durably consume a worst-case reservation and durably book the corresponding fill economics in separate SQLite transactions. A process failure between those commits could leave exposure reservation truth and economic truth inconsistent after restart.

## Implementation

- `DurableReservationBook.prepare_consume_mutation` prepares an immutable CONSUME event from one durable reservation-journal cut without mutating authority state.
- `DurableProviderEconomicBook.prepare_batch_mutation` prepares one immutable economic batch from one durable economic-book cut without mutating ledger state.
- `commit_economic_batch_with_reservation_consumption` validates that both authorities share the same JournalStore, account and environment and commits both aggregate events through one `JournalStore.commit_command` SQLite transaction.
- Aggregate-version fences already enforced by JournalStore reject concurrent writers before either member is inserted.
- Exact acknowledgement-loss replay is idempotent only when both durable sides already contain the same prepared facts.
- A one-sided pre-existing state is treated as corruption/incomplete integration and fails closed; the barrier does not silently complete the other side.
- The whole-simulator risk→authority→reservation→dispatch→fill→economics→reconciliation scenario now uses the durable provider economic book and this shared commit barrier.

## Focused evidence

`mvp/tests/test_atomic_fill_financial_commit.py` covers:
- restart reconstructs reservation consumption and economic fill together;
- injected failure before the shared commit persists neither side;
- acknowledgement loss after the shared commit can be retried without duplicate consumption or economics;
- economics-only and reservation-only partial durable states fail closed;
- an idempotency replay cannot change reserved usage;
- cross-account/environment composition is rejected before financial mutation.

The existing whole-simulator test exercises the barrier with the simulated provider and a third-currency-capable canonical fill booking path.

## Explicit residual scope

This increment proves persistence atomicity, not the economic mapping from every provider fill to every reservation resource. Remaining WP-14/WP-15 work includes provider-normalized fill/fee/slippage consumption mapping for all supported asset families, settled/unsettled cash and valuation freshness, derivative lifecycle economics, broader concurrent stress, real-provider qualification and exact-head cross-platform verification.

No LLM/model output receives financial authority. ACK remains distinct from fill. UNKNOWN remains blocking. No live credentials or real financial transfer are introduced.
