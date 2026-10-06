# Section 2 dependency/provenance convergence — 2026-10-06

## Current live basis

- Section: 2 — dependencies / rights / provenance / supply chain.
- Canonical work package: WP-03 dependency-policy.
- Canonical convergence PR: #2202.
- Exact convergence base: `main@8495a66cf7d8979c5e8b3811c2088523ed147bf1`.
- #2202 is the single current-main successor that supersedes stacked #2197 and #2199.
- Its initial convergence tree is byte-identical to #2199 tree `45549029aabfb0f2e9a90d3d5a00a2f22a9923cf`.

Section 0 remains closed and is not reopened by this work.

## Current-main authority already present

Current main plus #2202 contains the canonical WP-03 source-side dependency and NuGet-rights authority:

- strict NuGet lock parsing and exact target-aware Direct/Transitive graph identities;
- exact NuGet SHA-512 content hashes;
- locked restore/project coverage checks;
- imported MSBuild PackageReference fail-closed boundaries;
- exact .NET SDK `10.0.100` with roll-forward disabled;
- hash-locked Python development dependencies;
- explicit Python `3.12.10` and explicit GitHub-hosted OS routing in canonical workflows;
- deterministic release dependency manifest generation/checking;
- Desktop.Client transitive WebView2 lock synchronized to the Desktop direct lock;
- exact WebView2 package identity represented in the generated release dependency manifest;
- path-safe restored NuGet identity resolution;
- exact restored `.nupkg` SHA-512 verification against the lock graph;
- nuspec id/version plus reviewed license/notice bound to the exact restored artifact;
- canonical `NUGET_PACKAGES` authority;
- locked restore and restored-rights verification required in the same actual GitHub Actions job;
- bypass controls, duplicate/missing verification targets and post-verification re-restore fail closed;
- restored package-rights verification covers both AutoTrade.Contracts and AutoTrade.Desktop;
- WP-64 supply-chain qualification primitives already exist and remain the terminal release/supply-chain trust owner.

Historical #2141, #2188, #2197 and #2199 are not separate merge authorities after #2202 convergence.

## Source-side defects closed by the #2202 lineage

1. Desktop.Client's transitive Microsoft.Web.WebView2 lock no longer disagrees with the Desktop direct lock.
2. The release dependency manifest no longer falsely reports an empty .NET package graph while WebView2 is referenced.
3. Focused regression coverage prevents Desktop.Client lock identity/content-hash drift.
4. The WP03 gate no longer claims mutable Python `3.12` selection; canonical workflows use exact `3.12.10`.
5. Restored NuGet rights are checked against the actual locked package bytes, nuspec identity, reviewed license and notice rather than filename presence or sibling hash text alone.
6. Package-bearing projects cannot satisfy the gate by placing restore and rights verification in different jobs or by attaching skip/failure-suppression controls to the verifier step.
7. Section-2 current source state is durably recorded in one current-main convergence lineage.

No dependency, license, advisory, model/data right or release state is promoted to APPROVED merely by these source controls.

## Remaining Section-2 blockers

### Release composition / mapping

- exact final release composition evidence is absent;
- no authenticated mapping yet binds the actually distributed release component set to the corresponding `provenance/components.json` identities;
- until that mapping exists, the complete inspected-component catalog remains fail-closed qualification scope and must not be silently narrowed.

### Rights / external evidence

- exact model/data/news use and redistribution rights evidence is absent;
- Autosport first-party migration is owner-authorized for development, but complete contributor/release-distribution rights chain evidence remains unresolved;
- Nika Core is semantic adaptation rather than a runtime dependency, but release-rights provenance remains unresolved;
- inspected LEAN, WhiteBit.Net, CryptoExchange.Net and Alpaca candidates remain release-blocked until an exact selected composition plus dependency-graph, required-notice and rights evidence exists.

### Advisory / frozen-release evidence

- exact dependency-advisory qualification evidence is absent;
- no final frozen-release SBOM artifact is materialized and independently bound to the delivered release;
- terminal advisory/supply-chain PASS remains owned by the existing WP-64 signed qualification authority and must not be replaced by a WP-03 self-attestation.

These are blockers, not permission to infer rights from public repository visibility or to manufacture a permissive license.

## Completion rule

Section 2 is DONE only when the canonical selected/distributed scope is explicit and every actually imported or distributed byte has:

- exact source/package identity and lock;
- rights/license basis and required notices;
- exact release-composition mapping to provenance identity;
- advisory review bound to the exact dependency graph;
- model/data/news rights where applicable;
- deterministic SBOM/provenance evidence bound to the exact frozen release;
- no unresolved rights represented as APPROVED.

Provider/PAPER/LIVE, economic-edge, signed-release authorization and physical NVDA qualification remain separate gates.
