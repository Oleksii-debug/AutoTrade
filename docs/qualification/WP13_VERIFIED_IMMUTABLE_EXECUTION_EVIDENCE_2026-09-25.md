# WP-13 verified immutable execution evidence

Date: 2026-09-25  
Base main: `140b35536bd3992b981603492a174a6b6108614a`

## Scope

This convergence is the canonical successor to PR #675 and does not create a second execution-qualification authority.

The existing `ExecutionModelQualification` remains the single WP-13 qualification record. Qualified replay no longer accepts a caller-supplied evidence digest. Instead it requires:

- the canonical `ArtifactStore`;
- an exact evidence artifact UUID;
- the frozen expected evidence SHA-256 stored in the qualification.

The validator resolves the artifact through the canonical store, requires manifest integrity binding, verifies the immutable object through the store, and compares the resolved object digest against the frozen qualification. The artifact UUID is also frozen in the qualification, so a second manifest pointing at identical bytes cannot silently rebind the evidence identity.

## Fail-closed regressions

Focused tests cover:

- valid resolved immutable evidence;
- qualification digest versus actual resolved object mismatch;
- a different store containing different bytes under the same artifact UUID;
- missing/unresolvable artifact;
- alias artifact UUID with identical bytes;
- malformed artifact UUID/digest;
- all pre-existing model/protocol/instrument/purpose and independent-oracle gates.

## Authority boundary

This change is scientific/replay qualification only. It adds no provider credential handling, live send path, money movement, trading permission, promotion bypass or economic-edge claim.

Exact-head CI remains required before integration.
