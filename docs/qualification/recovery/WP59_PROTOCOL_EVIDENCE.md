# WP-59 recovery qualification evidence binding

This qualification surface is read-only. It does not restore a host, send a provider request, reacquire credentials or grant trading authority.

A recovery scenario can reach PASS only when its evidence is bound to:
- the exact source commit and release artifact hash;
- the qualification evidence schema version;
- one frozen recovery protocol identity;
- a concrete test-run identity;
- the required test identifiers for that scenario;
- immutable evidence references whose bytes and declared bindings can be integrity-checked.

The six required scenarios remain power loss, network loss, storage loss, session loss, split-brain attempt and upgrade failure. Existing financial invariants remain fail-closed: data loss, duplicate external actions, unresolved UNKNOWN submissions, incomplete reconciliation, broken journal/backup integrity, missing fencing, failed rollback or unqualified open-risk protection block qualification.

## Unresolved limits

Scenario evidence must explicitly carry unresolved limits. Any non-empty unresolved-limit set prevents a full PASS and leaves the decision INCONCLUSIVE unless a harder failure already makes the result FAIL. This prevents a successful narrow test from being presented as proof of a broader recovery property.

## Boundary

This increment strengthens evidence semantics only. It is not delivered-artifact recovery evidence and does not complete WP-59. Full qualification still depends on accepted WP-48, WP-49, WP-50 and WP-57 artifacts and measured scenario runs against the delivered release.


## Trust boundary

`ArtifactStore` is a content-integrity mechanism, not an independent verifier identity. A caller can create a store and publish receipts whose metadata repeats its own PASS assertions. Therefore a complete store-backed scenario set remains `INCONCLUSIVE` with `independent_evidence_trust_unavailable` until WP-59's release dependencies provide a qualified authenticated or cryptographically signed attestation boundary whose producer/verifier identity is not controlled by the evidence submitter. Hard recovery-invariant violations still produce `FAIL`.
