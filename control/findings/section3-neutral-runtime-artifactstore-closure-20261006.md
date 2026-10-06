# Section 3 neutral runtime + sealed ArtifactStore closure — 2026-10-06

## Scope

Section 3 covers the neutral production runtime primitives and the sealed content-addressed ArtifactStore authority.

Canonical work packages:
- WP-04 — neutral first-party runtime primitives;
- WP-06 — immutable content-addressed artifact store.

Both are complete on main.

## Neutral runtime ownership

Canonical production authority:
- `autotrade_runtime.strict_json`
- `autotrade_runtime.resource_lock`
- `autotrade_runtime.artifacts`

Research package names are compatibility aliases to the same runtime objects. They are not a second authority.

The neutral runtime identity regressions prove:
- product-only import has no research dependency;
- product-first and research-first imports resolve the same ArtifactStore class/module objects;
- repo-qualified research imports resolve to the same neutral authority;
- strict-JSON helper/error identities are shared;
- isolated product staging runs with `python -I -S` and no research module load.

## WP-04 evidence

Historical exact-head qualification:
- PR #843: baseline SUCCESS, research-primitives SUCCESS, Verify AutoTrade SUCCESS;
- merged PR #1022: baseline SUCCESS, research-primitives SUCCESS, Verify AutoTrade SUCCESS.

Current main additionally includes:
- 13 durable-publication hardening tests;
- 22 ResourceLock race/Windows path-fence tests;
- bounded JSON document size, nesting, decoded-node and integer domains;
- exact built-in str/bytes ingress;
- float-domain hardening that rejects:
  - nonzero numeric underflow to 0.0;
  - excessive floating significand digits;
  - excessive exponent digits;
  - non-finite decoded float results;
  while preserving canonical zero and the smallest positive subnormal.

Canonical 2026-10-06 float repair:
- `a2dbfa41702837163fde3dec6cdf7b1b92a3b290`
- `54e8eed276296ae8f4d27e62453d06e9c97e4aa6`

PR #2178 was closed as superseded because its valid fix targeted the obsolete research implementation; the repair was moved to the canonical neutral runtime.

## WP-06 evidence

Selected sealed-reader source:
- PR #1098 exact head `55d97774e8c3225374f1fff9cdb0c62f7ee157eb`
- baseline SUCCESS;
- research-primitives SUCCESS;
- full dual-OS Verify AutoTrade SUCCESS.

Neutral runtime materialization:
- PR #1313 is a direct ancestor of current main;
- exact 30-file runtime component;
- hermetic exact-source staging;
- publish + authenticated snapshot + sealed trusted read + audit in isolated Python;
- product-only import without research package;
- single canonical ArtifactStore/module identity.

Current main contains stronger post-materialization authority:
- exact built-in text/root ingress;
- publication-bound generation-pinned trusted reader;
- root/manifests/objects/staging generation continuity;
- constructor-free trusted execution view;
- crash-atomic publication and manifest state;
- retained namespace and recovery authority on POSIX and Windows;
- fail-closed symlink/reparse/hard-link/path replacement checks.

Current corpus includes:
- 55 core ArtifactStore tests;
- crash-atomic manifest tests;
- crash-atomic publication tests;
- retained recovery tests;
- generation-bound read tests;
- 18 trusted-reader hostile-caller/root-replacement tests;
- Windows publication/namespace/transaction/retained-rename tests.

WP-06 acceptance criteria are explicitly exercised by:
- `test_restart_after_manifest_commit_exposes_only_verified_complete_artifact`;
- `test_recovery_removes_only_unreferenced_objects`;
- missing/corrupt object and manifest tests;
- rights-aware export and independent export-authorization tests;
- hard-kill prepared/committed publication tests.

The #1313 hosted suites failed only later in unrelated learning/update-producer tests; artifact and WP-04 suites had already passed on Ubuntu and Windows. This is not an ArtifactStore failure.

## Supersession

Historical Section-3 closure PR #1573 is fully absorbed by main:
- 38 effective paths inspected;
- 33 byte-identical;
- 5 present as newer, stronger versions;
- 0 missing paths.

The five stronger current-main paths are:
- `autotrade_runtime/artifacts/_root_authority.py`;
- `autotrade_runtime/artifacts/_root_authority_failure_fix.py`;
- `research/pyproject.toml`;
- `provenance/release-dependency-manifest.json`;
- `research/tests/test_artifact_authority.py`.

PR #1573 was closed as superseded.

## Qualification boundary

Section 3 completion establishes neutral runtime/storage integrity authority only.

It does not establish:
- provider truth;
- PAPER/LIVE trading authority;
- profitability or economic edge;
- signed release qualification;
- physical Windows/NVDA qualification;
- terminal qualification-policy/release trust.

Issue #1074 remains a downstream qualification/release trust concern and is not a blocker for WP-06 storage-TCB closure.
