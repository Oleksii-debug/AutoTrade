# Section 2 dependency/provenance convergence — 2026-10-06

## Current live basis

- Section: 2 — dependencies / rights / provenance / supply chain.
- Canonical work package: WP-03 dependency-policy.
- Exact starting main: `536002f84421eb073e267084ab60bd5829473485`.
- Current repair PR: #2197.
- Current repair head at this finding: `4a4822c6425a9e1100ca73a26a8a6bb737b3cc14`.

Section 0 remains closed and is not reopened by this work.

## Current-main authority already present

Current main already contains materially newer WP-03/supply-chain authority than historical #2141/#1942:

- strict NuGet lock parsing and exact target-aware Direct/Transitive graph identities;
- exact NuGet SHA-512 content hashes;
- locked restore/project coverage checks;
- imported MSBuild PackageReference fail-closed boundaries;
- exact .NET SDK `10.0.100` with roll-forward disabled;
- hash-locked Python development dependencies;
- explicit Python `3.12.10` CI pinning on canonical workflows;
- WebView2 package-rights policy and restored-package license verification;
- deterministic release dependency manifest generation/checking;
- WP-64 supply-chain qualification primitives binding SBOM/provenance/dependency-lock/component/rights/advisory evidence to exact release identity.

Historical #2141 is superseded and was closed rather than replayed onto this stronger current-main authority.

## Concrete defects repaired by #2197

1. `tests/Desktop.Client/packages.lock.json` was stale at Microsoft.Web.WebView2 `1.0.4191.47` while the referenced Desktop project and direct lock use `1.0.4258.31`.
2. The committed release dependency manifest reported no .NET package dependencies although current source has an exact WebView2 PackageReference.
3. No focused repository regression required Desktop.Client's transitive WebView2 lock to match the Desktop direct lock identity and NuGet content hash.
4. The WP03 qualification document still claimed mutable Python `3.12` workflow selection even though canonical workflows already use exact `3.12.10`.

The repair retains all fail-closed legal/advisory/release blockers. No missing evidence is converted into PASS/APPROVED.

## Remaining Section-2 blockers

### Source / composition work

- exact release-composition evidence is absent;
- exact model/data/news rights evidence is absent;
- exact dependency-advisory qualification evidence is absent;
- no frozen-release SBOM artifact is materialized;
- the current rights gate treats the complete inspected-component catalog as qualification scope. Do not weaken that gate until an explicit authenticated mapping exists from release-composition distributed components to provenance component identities.

### Rights / external evidence

- Autosport first-party migration is owner-authorized for development, but the complete contributor/release-distribution rights chain remains unresolved;
- Nika Core is semantic adaptation rather than a runtime dependency, but its release-rights provenance remains explicitly unresolved;
- inspected LEAN, WhiteBit.Net, CryptoExchange.Net and Alpaca candidates remain release-blocked until an exact selected composition plus dependency-graph, notice and advisory evidence exists.

These states are blockers, not permission to invent a permissive license or infer rights from public repository visibility.

## Completion rule

Section 2 is not DONE merely because parsers/tests exist. Closure requires the canonical WP-03 gate to represent the exact selected dependency/reuse scope and every actually imported/distributed byte to have:

- exact source/package identity and lock;
- rights/license basis and required notices;
- advisory review bound to the exact dependency graph;
- model/data/news rights where applicable;
- deterministic SBOM/provenance evidence bound to the exact release source;
- no unresolved rights represented as APPROVED.

Provider/PAPER/LIVE, economic-edge, signed-release and physical NVDA qualification remain separate gates.
