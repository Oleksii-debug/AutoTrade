# Section 2 dependency/provenance closure candidate — 2026-10-06

## Scope

Section 2 owns dependency, rights, provenance and supply-chain source authority. It does not grant signed-release, provider/PAPER/LIVE, NVDA, profitability or economic-edge authority.

Current canonical successor: PR #2237.

## Current-main basis

The successor was reconstructed directly on current main after Section 0/1/3/4/5 convergence rather than rebasing stale stacked Section-2 branches.

Section 3 prerequisite is already closed by live repository authority:
- neutral strict JSON: `autotrade_runtime.strict_json`;
- neutral sealed ArtifactStore: `autotrade_runtime.artifacts`;
- research artifact names are compatibility aliases, not a second implementation.

## Dependency graph authority already retained from main

Current main already provides:
- exact target-aware NuGet lock parsing;
- malformed, duplicate and ambiguous lock rejection;
- rejection of imported/root/source MSBuild PackageReference or restore-authority injection;
- inspectable one-line restore commands with locked-mode/target validation;
- exact package-bearing project restore coverage;
- validated `dotnet_locked_dependency_graph`;
- deterministic release dependency manifest generation from that graph;
- exact dependency-advisory evidence binding;
- fail-closed composition and rights blocker inventory.

Those newer main blobs are intentionally preserved instead of replaced by historical #1611/#2141 copies.

## Missing authority reconstructed by #2237

### Restored NuGet rights
- one canonical `NUGET_PACKAGES` authority;
- restored-rights verification occurs in the same real Actions job as restore;
- non-job, cross-job, conditional, continue-on-error and custom-shell spoofing fail closed;
- restored nupkg SHA-512, nuspec id/version, license and notice bytes are verified;
- package files must be exact non-symlink regular files under the exact package root.

### Durable supply-chain proof
- exact detached `SupplyChainEvidence` graph;
- canonical strict-JSON proof bytes and canonical parser;
- signed semantic-subject digest;
- exact delivered release artifact UUID/SHA binding;
- one held authenticated ArtifactStore snapshot per evidence artifact;
- independent held-byte SHA-256 verification;
- caller signed receipt detached before canonical verifier calls;
- independent-review and semantic-subject checks must resolve to the same accepted attestation/policy/trust-root;
- accepted trust and delivered-artifact identities are retained in the qualification result.

## Evidence boundary

The source authority remains fail-closed for real-world inputs. Unresolved first-party distribution rights, model/data rights, final release composition, advisory review or candidate dependency approval remain BLOCKED/INCONCLUSIVE until actual evidence exists. Section 2 closure does not fabricate those facts; it guarantees they cannot silently become PASS.

## Closure rule

Section 2 becomes DONE only when:
1. PR #2237 remains current-main, mergeable and review-clean;
2. exact-head applicable CI is terminal green; queued/running/cancelled/stale-head is not PASS;
3. merge completes on the exact accepted head;
4. post-merge main readback confirms the accepted Section-2 source tree;
5. canonical control state records Section 2 source authority complete without changing external release/evidence claims.
