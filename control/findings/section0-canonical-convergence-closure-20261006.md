# Section 0 canonical convergence closure — 2026-10-06

## Canonical result

Section 0 canonical convergence is integrated on `main`.

- Final convergence PR: #2148
- Accepted candidate head: `9ccd9bc6f6042e5362f50d4d6d003828b85da3ab`
- Accepted base: `3203af2307e90ce142c2b1adf7f116eee7c08785`
- Merge commit: `e13c8fd099aca5998f2fc10b43bcb81aa5fb2539`
- Expected candidate tree: `7ce853b2bba07d29291a6305eeb552e92086a283`
- Post-merge main tree: `7ce853b2bba07d29291a6305eeb552e92086a283`
- Post-merge tree equality: PASS

## Non-loss convergence proof

The final three-way convergence used the historical common base, the accepted current main, and the prior Section-0 candidate.

- 428 paths changed only by Section 0 and were retained.
- 22 paths changed only by newer main and were retained from main.
- 42 paths converged to the same changed blob on both lineages.
- 29 true two-sided overlaps were resolved in favor of newer current-main ownership, preserving already-integrated Section 1 / Section 6 authority.
- Current main to accepted candidate: 435 changed paths.
- Base-path deletions: 0.
- Object-type changes: 0.
- Symlinks: 0.
- Gitlinks/submodules: 0.
- Suspicious/case-colliding paths: 0.
- Two reviewed executable-bit changes only: `tools/check_nvda_qualification.py` and `tools/check_product_completion.py`.

## Reconvergence trust root

The accepted tree contains the hardened existing reconvergence-integrity authority rather than a parallel guard.

- guard blob: `0ac00135b58f6c2d11ccb2c62e861caca1b1f8fe`
- focused regression blob: `ce7a7850dcced4c3b350369f6e0b7a0d27a3cd02`
- exact-head OWNER approval existed for:
  - `.github/workflows/reconvergence-integrity.yml`
  - `control/tools/reconvergence_integrity.py`
- checked-in qualification workflows are protected as sentinels;
- ordinary modification authority for executable reconvergence trust roots remains exact-path and externally issued;
- stale-target-base, sparse-tree, destructive replacement, malformed Git status/path and trust-root replacement checks remain fail-closed.

## Supersession

PR #2177 was a phase-1 conflict-free probe. Recursive Git-tree comparison proved it was a strict subset of the final #2148 result:

- #2177 changed 428 paths;
- #2148 retained all 428;
- #2177 had zero unique changed paths relative to #2148;
- #2177 was closed as superseded.

## Qualification boundary

This closes Section 0 canonical source/integration convergence. It does not claim provider, PAPER/LIVE, economic-edge, signed-release, physical Windows/NVDA, or whole-product qualification.

GitHub-hosted exact-head jobs for the final candidate remained queued because runners were not assigned. They were not represented as PASS. The repository-level source tree and canonical integration are nevertheless closed by the exact expected-head merge and post-merge tree equality proof above. External branch/ruleset enforcement tracked by issue #642/#543 remains a separate control-plane deployment boundary.
