# Section 2 dependency/provenance convergence — 2026-10-06

## Canonical source/integration result

Section 2 source and integration authority is integrated on `main`.

- Canonical final PR: #2264
- Accepted candidate head: `7f2db08bcd748ad79b8bcb9e06fb33c635269be1`
- Merge commit: `458cd7bb43b8457e0bb0593b523f54e35f5cd54d`
- Accepted candidate tree: `25008a8980cc9c2beac8f114a6fe897a58fafcb4`
- Post-merge main tree at the integration cut: `25008a8980cc9c2beac8f114a6fe897a58fafcb4`
- Post-merge tree equality: PASS
- Historical #2141/#2188/#2197/#2199/#2200/#2202/#2204/#2260/#2262 are provenance/superseded integration lineages, not separate merge authorities.

## Integrated WP-03 authority

The merged tree contains one fail-closed dependency/provenance path for the selected source/distribution inputs:

- exact .NET SDK/runtime selection and locked NuGet Direct + Transitive graph identity;
- exact NuGet SHA-512 content identities;
- locked restore/project coverage and imported-MSBuild dependency fences;
- exact restored package bytes, nuspec id/version, reviewed license and required notice verification;
- canonical `NUGET_PACKAGES` authority and same-job restore/rights verification;
- deterministic release dependency manifest generation;
- Desktop/WebView2 lock and manifest convergence;
- exact distributed/frozen composition -> provenance identity mapping through `tools/release_scope_mapping.py`;
- exact SBOM source/application binding and dependency reachability;
- package-rights and external-runtime/reuse scope closure;
- provider-free frozen dependency/provenance binding;
- canonical WP-64 detached supply-chain proof bytes, strict proof ingress, semantic-subject binding and delivered-artifact anti-substitution identity;
- canonical signed qualification verifier remains the only terminal trust root.

## Issue #2203 — CLOSED

The release-composition/provenance mapping residual is implemented and issue #2203 is closed as completed.

The mapping authority rejects missing, duplicate, ambiguous, caller-renamed, wrong-hash and orphan mappings; binds exact source/composition/SBOM/dependency-lock identity; maps exact WebView2 package/version/hash to reviewed rights evidence; retains explicit non-runtime/source-semantic classifications; and keeps unresolved first-party distribution rights fail-closed.

## External evidence still required

These are not source-code defects and must not be manufactured:

1. **First-party release-distribution rights.**
   Autosport migrated source remains development-authorized but lacks a complete contributor/release-distribution rights chain. Nika Core is a semantic reference rather than a runtime dependency, but its release-rights provenance remains unresolved wherever it remains in qualification scope.

2. **Model/data/news rights.**
   Exact use and redistribution evidence must exist for any such material included in the selected final distribution. If the final frozen composition contains none, that NOT_APPLICABLE conclusion must itself be derived from exact composition evidence rather than asserted by a caller.

3. **Production qualification trust material.**
   The canonical packaged qualification-policy pin remains intentionally unavailable until independently reviewed production `qualification_trust_policy.json` bytes/root material exist. Section 2 does not create a signer/root merely to make a gate green.

4. **Dependency advisory evidence.**
   Exact advisory review must be bound to the exact dependency graph and accepted through the existing WP-64 trust authority. A hand-written candidate JSON or public-search result is diagnostic, not terminal PASS.

5. **Final frozen-release SBOM/provenance.**
   A final SBOM/provenance artifact must be materialized from the actual delivered/frozen release and bound to that exact artifact identity. Source-side mapping infrastructure is complete; the final release artifact does not yet exist.

## Qualification boundary

The source/integration portion of Section 2 is complete. Terminal WP-03 / Section-2 evidence remains IN_PROGRESS until the external rights, trust-policy, advisory and frozen-release facts above exist and are independently bound.

Queued/pending/cancelled hosted jobs are not PASS. No provider/PAPER/LIVE, profitability/economic-edge, signed-release authorization or physical Windows/NVDA authority follows from this source integration.

## Completion rule

Section 2 may move to DONE only when every actually imported or distributed byte in the final selected composition has exact identity/lock, rights/license basis and notices; advisory review is bound to that exact graph; model/data/news rights are either evidenced or composition-proven not applicable; the production qualification trust policy/root is independently reviewed and pinned; and the final frozen SBOM/provenance is bound to the delivered release. Unresolved rights never become APPROVED by inference.
