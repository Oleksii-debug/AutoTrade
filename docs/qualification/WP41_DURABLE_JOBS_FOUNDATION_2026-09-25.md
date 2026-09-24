# WP-41 durable jobs foundation — 2026-09-25

Exact base main: `39ef7063bc17c96eb0eb51d6dccbfb24bc1e563a`.

This increment adds one SQLite-backed durable job authority for non-financial
background research, learning and agent work. It performs no financial sends and
grants no trading authority.

## Implemented invariants

- Explicit durable lifecycle: QUEUED → CLAIMED → RUNNING → SUCCEEDED / FAILED / CANCELLED.
- Stable job ID + version plus globally unique dedupe key.
- Explicit immutable input hashes and canonical JSON payloads.
- Checkpoints are durable and restart-visible only from a valid RUNNING lease.
- Claim records attempt count, owner identity, owner epoch, monotonic fencing token,
  lease expiry and resource reservation units.
- Workers can claim only jobs within the caller's resource ceiling.
- Expired CLAIMED/RUNNING work requeues only while its bounded attempt budget remains;
  otherwise it becomes FAILED.
- Old owner epochs and old fencing tokens cannot heartbeat, checkpoint, fail or publish.
- One immutable accepted result per job ID + version; exact replay is a no-op and a
  different second result fails closed.
- WAL + FULL synchronous SQLite durability and foreign keys are enabled.
- Result UPDATE/DELETE is blocked by SQLite triggers.
- Non-finite JSON payloads, checkpoints and results are rejected.

## Focused evidence

`mvp/tests/test_durable_jobs.py` covers identity/dedupe conflicts, explicit lifecycle,
restart checkpoint recovery, owner-epoch fencing, expired-lease recovery, single result,
resource ceilings, bounded retries, cancellation and non-finite inputs.

## Remaining before whole-product WP-41 PASS

This is still a foundation until real producers/consumers use this authority and
whole-product qualification demonstrates cancellation propagation, resource release,
artifact compare-and-swap/publication semantics, and that financial sends never enter
this generic retry loop. Exact-head CI is also required before integration.
