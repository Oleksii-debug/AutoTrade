# Section 2 closure candidate

Date: 2026-10-04
Parent: Section 3 current-main ArtifactStore closure candidate
Base parent head: `75e89851031558145ee5c8a895493487ae7eec09`

This branch converges two already-reviewed dependency/provenance lineages into one Section 2 closure lane:

1. strict NuGet lock and dependency-composition provenance;
2. durable canonical supply-chain qualification/proof authority.

## Included semantics

Dependency graph and restore:
- strict target-aware NuGet lock parsing;
- malformed/duplicate/ambiguous lock content fails closed;
- imported/root/source MSBuild PackageReference injection is rejected until explicitly supported;
- package-bearing release projects must be covered by locked restore commands;
- dependency-control/lock changes trigger the .NET foundation gate.

Provenance and supply-chain:
- release provenance consumes the validated dependency graph rather than filename presence;
- canonical supply-chain evidence binds source/build, delivered artifact identity, SBOM/provenance/locks, inventories, component identities, and data/model rights;
- durable proof bytes use bounded strict JSON and canonical byte identity;
- equivalent but noncanonical encodings fail closed;
- trust is resolved through the canonical qualification attestation path.

## Dependency

The durable proof implementation uses the neutral sealed ArtifactStore/runtime supplied by the Section 3 current-main closure candidate. Therefore this PR is intentionally stacked on Section 3 until that parent is accepted; it must then be retargeted/reconverged to accepted main and requalified.

## Closure rule

Section 2 is not DONE until:
1. Section 3 prerequisite is accepted;
2. this branch is retargeted/reconverged onto accepted main;
3. exact-head baseline, dependency/.NET and full Verify gates are terminal green;
4. review state is clean;
5. merge completes;
6. post-merge readback confirms the accepted source identity.

No provider/PAPER/LIVE, release-signing, profitability, economic-edge or NVDA qualification authority is granted by this source closure.
