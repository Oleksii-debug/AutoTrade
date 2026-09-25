# First-party reuse: Autosport swarm ownership → AutoTrade registry control

Source snapshot: `Oleksii-debug/Autosport@cb102d85f0c820c7097875191deca73e53ec94f5`.

Inspected source:
- `scripts/swarm_claim_state.py` blob `c801dbf6b79dadf1c22217a3e858a15cf1823f23`
- `scripts/swarm_claim_state_impl.py` blob `c6f2711d3f1aeb3b8a7add74ea1eb0ccd96e2ea2`
- `scripts/swarm_branch_lease_guard.py` blob `a1d14db9a5a905b684634d34e8cf193468e5bf70`
- associated claim/lease/branch-guard tests at the same revision.

## Reused invariants

AutoTrade adopts the domain-neutral fail-closed invariants:
- malformed ownership evidence never grants mutation authority;
- leases expire deterministically;
- stale state/generation cannot renew or release new authority;
- duplicate/idempotent requests cannot silently change identity;
- a foreign writer under a live mutation owner is a collision;
- branch movement must have contiguous parent evidence bound to current head;
- read-only work does not become source-mutation authority.

## Deliberate change

Autosport's historical claim resolver folds GitHub issue comments and can apply task capacity. AutoTrade does **not** make comments or labels canonical ownership authority and does not introduce a global worker-capacity gate.

AutoTrade instead uses its dedicated `control/registry` branch with generation/CAS semantics and overlapping
`AUTHORITY_FAMILY + SEMANTIC_KEY + MUTATION_SCOPE` exclusion. The imported branch-lease logic is adapted to consume that registry state.

No sports semantics were imported.
