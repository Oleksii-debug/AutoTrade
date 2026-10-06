# WP-01 canonical contracts closure evidence — 2026-10-06

Status: IMPLEMENTATION_COMPLETE_PENDING_EXACT_HEAD_HOSTED_CI

This record is intentionally fail-closed. It does not declare WP-01 complete until the exact integration head is terminal green, current-main topology is rechecked, and the qualified contract result is merged.

## Canonical integration candidate

- pull request: #1440
- branch: `wp01/dataset-manifest-v6-current-main-20261004-sol56`
- exact head at this evidence cut: `68b20a2c462cdab9ef900cb04cd9da60b78f6b08`
- exact base / merge-base at this evidence cut: `main@60e7c95b3b572810dcfb6c4ab34e0b338b0ace02`
- topology: ahead-only, behind 0
- effective diff: 47 paths
- contract version: `6.0.0`

## WP-01 authority present on the candidate

1. JSON Schema Draft 2020-12 contract surface remains one versioned package.
2. Python, C# and TypeScript common-scalar bindings use the same generated contract/corpus authority.
3. Decimal wire admission is schema-owned and bounded at 256 significant digits, scale 256 and integer magnitude 256; generated consumers do not redefine it through native floating/decimal ranges.
4. Contract version governance treats changes to existing definitions, required members, Decimal envelope metadata, OpenAPI operations and authentication/security semantics as breaking and requires a new major.
5. Terminal-newline scalar behavior is cross-language consistent.
6. Host OpenAPI authentication/security semantics are version governed and canonicalized independent of YAML mapping/order-only changes.
7. DatasetManifest v6 requires non-empty `content_hashes` and `source_evidence`.
8. Canonical semantic validator `dataset-manifest-content-authority-v1` requires every declared content digest to be covered by an exact `source_evidence[].sha256` match; extra evidence is provenance-only.
9. The DatasetManifest semantic rule is implemented equivalently in Python, installed Python, C# and TypeScript and exercised by one shared 13-case corpus.
10. The installable exact-numeric package is versioned `0.0.2`; research pins exactly `autotrade-exact-numeric==0.0.2`, preventing an old v5 0.0.1 wheel from satisfying the v6 dependency.
11. The Host event resume contract binds OpenAPI `after` and `UiSnapshot.event_cursor` to canonical `Sequence`. Runtime Host ingress enforcement remains WP-43 scope and is not claimed by WP-01.

## Semantic blast-radius readback

Compared with exact current main at the evidence cut:

- 10 of 12 JSON Schema documents change only their versioned `$id` from 5.0.0 to 6.0.0.
- `data.schema.json` additionally changes only DatasetManifest authority: `source_evidence.minItems=1`, the semantic-validator declaration and its normative comment.
- `ui.schema.json` additionally changes only `UiSnapshot.event_cursor` from unconstrained non-empty text to canonical `Sequence`.
- OpenAPI additionally changes only package version and `GET /api/v1/events?after=` from unconstrained string to canonical `Sequence`.
- The canonical DatasetManifest fixture gains exact non-empty source evidence matching its content hash.

No unrelated contract-domain semantic mutation was found in final source review.

## Merged historical WP-01 lineages verified as ancestors of main

The following previously open WP-01 work was mechanically verified as present on live main and its stale issues were closed as completed:

- schema-derived scalar corpus and semantic version governance (#693 / merged #719 lineage);
- top-level OpenAPI authentication/security governance (#726, #728 / merged #727 lineage);
- OpenAPI security semantic canonicalization (#732);
- non-secret UI session/Host API authentication contract (#700 / contract v3 lineage);
- scoped UiCommand contract v2 (#563);
- terminal-newline cross-language scalar convergence (#882 / #1069);
- canonical Decimal resource envelope and exact numeric authority (#1078 / #1125, #1131, #1132, #1133);
- SubmissionResult provider-contract convergence (#549 and its merged provider-contract repairs).

## Exact-head verification history

On exact predecessor head `713e0589f0e16d02cbd2834fecd4df2df2b52f0c`, with identical v6 contract payload:

- contracts: SUCCESS
- dotnet-foundation: SUCCESS
- baseline: SUCCESS
- research-primitives: SUCCESS
- Verify AutoTrade: FAILURE

The Verify failure was an orchestration defect, not a failing v6 semantic test: `tools/verify.py` invoked the v6 .NET contracts harness with only the legacy common-scalar corpus although that harness requires both the common-scalar and DatasetManifest semantic corpora.

The exact successor `68b20a2c462cdab9ef900cb04cd9da60b78f6b08` changes only:

- `tools/verify.py`: supplies both v6 corpora to the .NET contracts harness;
- `tests/Contracts/test_verify_orchestration.py`: regression-locks that full Verify wiring.

The targeted orchestration regression was independently reproduced as passing. This is supportive evidence only and does not replace hosted exact-head qualification.

## Terminal closure gates still required

Before WP-01 may be recorded COMPLETE:

1. exact-head `contracts` terminal SUCCESS;
2. exact-head `dotnet-foundation` terminal SUCCESS;
3. exact-head `baseline` terminal SUCCESS;
4. exact-head `research-primitives` terminal SUCCESS;
5. exact-head dual-OS `Verify AutoTrade` terminal SUCCESS;
6. current main re-read with candidate behind 0, or a path-safe reconvergence followed by fresh exact-head qualification;
7. no unresolved review blocker;
8. merge #1440;
9. close #1225 as completed;
10. update `control/work-packages/bank.json` and `control/qualification.json` from their stale WP-01 IN_PROGRESS state to exact merged evidence.

At this evidence cut the hosted workflows for `68b20a2...` exist but are queued. Queued is not PASS.

## Boundary

WP-01 completion does not qualify provider trading, PAPER/LIVE execution, economic edge, profitability, release packaging, NVDA usability, or the WP-43 runtime event-cursor ingress. Those remain owned by their respective work packages.
