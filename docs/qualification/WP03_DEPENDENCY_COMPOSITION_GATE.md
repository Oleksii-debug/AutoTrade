# WP-03 dependency composition gate

Status: **IN PROGRESS / FAIL CLOSED**.

This gate audits the checked source tree and deliberately refuses to call the release dependency composition qualified while exact dependency or rights evidence is incomplete.

Current exact-main blockers captured by the gate:

- first-party Autosport and Nika reuse still has unresolved release/distribution rights records;
- selected external components remain pending exact package/transitive composition and notice review.

The gate also verifies that the current Python runtime test requirements are exact-version entries and inspects every `PackageReference` under `src/` for exact version identity.

This is not an SBOM and does not approve any dependency. It is a deterministic blocker inventory that prevents a false WP-03 PASS and gives the remaining composition work a machine-checked boundary.

## Resolved in this lineage

The repository and both .NET qualification workflows now select SDK `10.0.100` exactly, with `rollForward=disable`. The isolated research build backend is also pinned to `setuptools==84.0.0`. These remove SDK feature-band and Python build-backend drift from WP-03 evidence; they do not resolve transitive package, rights, SBOM or notice blockers.


## Command modes

`python tools/check_dependency_composition.py` is report mode. It always emits the deterministic blocker inventory and remains usable while WP-03 is intentionally incomplete.

`python tools/check_dependency_composition.py --require-qualified` is the release-enforcement mode. It returns a non-zero exit code whenever any composition blocker remains. Release automation must use this strict form; report mode is not release approval.

## Current reproducibility state

The former mutable Python `3.12` CI blocker is resolved on current main. Baseline, contracts, control-plane, dotnet-foundation, futures, provider-free product, recovery, research, science, Verify AutoTrade and zero-model qualification now resolve Python through the exact patch runtime `3.12.10`; matrix workflows pin `python-version: ["3.12.10"]`. GitHub-hosted OS selection is also explicit for the canonical dual-OS matrices (`ubuntu-22.04` / `windows-2025`).

The remaining WP-03 blockers are evidence/composition boundaries, not mutable Python runtime selection:

- exact release composition evidence is absent;
- model/data/news rights evidence is absent;
- first-party Autosport/Nika release-distribution rights chain remains unresolved;
- inspected candidate external components remain release-blocked until exact composition/notice/advisory evidence exists;
- exact release dependency advisory evidence is absent;
- the generated release dependency manifest must stay synchronized with every package-bearing source project and committed lock graph.

These blockers remain fail-closed and must not be converted into APPROVED/PASS without the corresponding evidence.
