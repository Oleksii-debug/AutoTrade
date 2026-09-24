# WP-64 exact-release supply-chain qualification

This branch adds the independent release supply-chain qualification required by WP-64. It consumes, rather than replaces, the dependency/provenance, secret-boundary, CI and untrusted-input evidence produced by their owning packages.

## Fail-closed checks

The evaluator binds every review to the exact release commit and verifies:

- the build commit equals the declared release commit;
- the SBOM component inventory exactly matches the declared distributed dependency inventory;
- declared and observed artifact SHA-256 identities match;
- license and distribution-rights decisions are explicit;
- required notices are present;
- blocking advisories fail while unknown advisory state remains inconclusive;
- model/data rights are explicit and bound to the same release commit.

A review for an older architecture snapshot cannot approve a different release. Unknown rights or advisory state cannot become PASS.

## Authority boundary

A supply-chain PASS is qualification evidence only. The result has `release_authority = false`; release-candidate and whole-product authorities remain separate.

## Local verification

`python -m pytest -q tests/SupplyChain/test_supply_chain_qualification.py`

Result before publication: 9 passed. Exact-head CI on the GitHub branch remains required before integration.
