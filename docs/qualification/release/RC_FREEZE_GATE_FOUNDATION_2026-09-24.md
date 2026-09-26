# WP-54 release-candidate freeze gate foundation — 2026-09-24

Status: **freeze mechanism only; no real release candidate is frozen by this
change**.

## Authority boundary

This foundation does not build Windows artifacts, sign binaries, generate an
SBOM, decide dependency rights, perform NVDA testing, or run API compatibility
tests. Those remain owned by their canonical packages.

The gate consumes their exact evidence and permits a deterministic
release-candidate manifest only when all required evidence belongs to one exact
source SHA and explicitly passes.

Required roles are currently:

- HOST
- WEB
- DESKTOP
- WINDOWS_PACKAGE
- SBOM
- DEPENDENCY_RIGHTS
- ACCESSIBILITY
- API_COMPATIBILITY
- CLEAN_INSTALL
- LICENSE_NOTICES
- RELEASE_QUALIFICATION

This list is a closed set for the current freeze protocol. An unknown or extra
artifact role is a protocol error and requires an explicit schema/protocol review
before it can participate in a release candidate; it cannot be smuggled into the
manifest merely by supplying matching PASS metadata or a signed attestation.

HOST, WEB, DESKTOP and WINDOWS_PACKAGE must carry verified signatures. Every required artifact
must carry the same exact source SHA as the candidate. RELEASE_QUALIFICATION is
the exact-head PASS evidence produced by the canonical release-qualification
authority (WP-59/WP-54 integration boundary); the freeze gate does not duplicate
its provider, recovery, NVDA or security decisions. FAIL and INCONCLUSIVE are
both blocking. Any explicit unresolved blocker also prevents manifest
publication.

## Determinism

The frozen JSON manifest sorts artifact roles and uses canonical JSON encoding.
Its SHA-256 therefore changes when any bound artifact/evidence hash changes and
is stable when only input ordering changes.

A blocked candidate receives no frozen manifest hash. This prevents a partially
qualified package from acquiring an RC identity that could later be mistaken
for an accepted exact build.

## Repository evidence

- `mvp/autotrade_mvp/release_candidate.py`
- `mvp/tests/test_release_candidate_freeze.py`

The tests cover missing evidence, source-SHA mismatch, missing binary signatures,
failed accessibility, inconclusive dependency/rights or release-qualification
evidence, unresolved blockers, duplicate roles and deterministic manifest hashing.

## Still required for WP-54

WP-54 remains incomplete until the actual WP-50/51/52/53/64 artifacts and
evidence are accepted and supplied to this gate, then clean-install and
compatibility evidence is recorded against the exact delivered build. No
synthetic unit-test fixture in this foundation is a signed production release.
