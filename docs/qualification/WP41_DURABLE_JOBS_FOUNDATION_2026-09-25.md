# WP-41 durable jobs foundation — 2026-09-25

Base main: `93f9ba2773973bcd45b43a98ec49a8171a3603d0`.

This increment adds one SQLite-backed durable job authority for background research,
learning and agent work. It does not grant trading authority and does not perform
network sends.

## Invariants implemented

- One immutable accepted result per `job_id + version`.
- Durable PENDING/RUNNING/SUCCEEDED/FAILED/CANCELLED state.
- Fencing token increases on every claim; stale workers cannot heartbeat, fail or complete.
- Expired RUNNING jobs recover to PENDING until the configured attempt budget is exhausted.
- Attempt budgets are durable across restart.
- Exact duplicate enqueue is idempotent; conflicting reuse of a job identity fails closed.
- SQLite WAL + FULL synchronous durability boundary, with foreign keys enabled.
- Result rows are protected against UPDATE/DELETE by SQLite triggers.
- Non-finite JSON payloads/results are rejected.

## Focused tests

`mvp/tests/test_durable_jobs.py` covers duplicate identity, restart recovery,
stale-worker fencing, bounded retries, cancellation and non-finite input.

Repository CI on the exact PR head is required before integration. This foundation
does not claim full WP-41 completion until integration with bounded worker resources,
job producers/consumers and whole-product qualification evidence is demonstrated.
