# AutoTrade claim registry

Dedicated operational ownership branch.

Current state: **protocol implemented, not yet enabled for concurrent mutation**.

Canonical implementation:
- `main:control/tools/registry_state.py`
- `main:control/tools/branch_lease_guard.py`
- `main:tests/Control/test_registry_state.py`

Claim identity:
`AUTHORITY_FAMILY + SEMANTIC_KEY + MUTATION_SCOPE`.

Protocol:
- generation compare-and-swap semantics;
- non-force fast-forward branch updates are the external serialization boundary;
- explicit lease expiry and renewal;
- all-or-none mutation claim request;
- idempotent request IDs;
- stale generations cannot claim/renew/release;
- overlapping live mutation scope collides;
- read-only audit/research does not acquire mutation authority;
- branch movement must be attributable to the live owner with contiguous parent evidence;
- comments and labels are never ownership authority.

Activation gate:
concurrent mutation must remain disabled until cross-platform control-plane tests and the actual GitHub registry-branch update path are qualified at an exact main head.

## Finalization checkpoint

Main now includes `control/tools/registry_store.py`. Local real-Git tests exercise sibling-write exclusion, atomic state/log publication and re-read/retry without force. Parent/child path conflicts are detected across semantic labels, and every mutated path must be covered.

This storage primitive is intended for a trusted service. It does not implement authenticated identity, READY/dependency admission, per-claim fencing or branch protection. Both registry and main were unprotected at audit; mode remains disabled. See main `docs/engineering/16_BASELINE_FINALIZATION_AUDIT.md` and the local evidence record.
