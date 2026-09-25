# WP-35 protocol-registry foundation

Exact base: `d3fc46dd9be80d053ca21afb0d883b5500fc7cd1`.

This increment establishes a durable, fail-closed scientific registry for pre-registered protocols, all attempted trials, holdout accesses and locked evaluations.

## Implemented invariants

- protocol identity is immutable; exact re-registration is idempotent while changed content is rejected;
- all required protocol fields from the canonical scientific architecture are enforced before registration;
- binary floating-point values are rejected at frozen scientific evidence boundaries;
- failed, discarded and cancelled trials consume the registered trial budget and remain durable evidence;
- every holdout access is recorded before later reuse can be described as untouched;
- locked evaluation records the prior-access count and marks only the first access as untouched;
- canonical registry tables are append-only at the SQLite layer through UPDATE/DELETE rejection triggers;
- state persists across process restart under SQLite WAL mode.

## Evidence and limits

Focused regression coverage is in `research/tests/test_protocol_registry.py`. Exact-head CI is required before integration.

This is a WP-35 foundation, not a completion claim. Remaining work includes generated canonical contract binding for ExperimentProtocol/EvaluationResult, OS/process isolation around privileged holdout data, explicit fresh-segment allocation, integration with replay checkpoints and promotion gates, and exact-head cross-platform qualification. It grants no trading authority and does not establish economic edge.
