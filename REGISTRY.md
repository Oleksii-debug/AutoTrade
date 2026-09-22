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
