# WP-03 dependency composition gate

Status: **IN PROGRESS / FAIL CLOSED**.

This gate audits the checked source tree and deliberately refuses to call the release dependency composition qualified while exact dependency or rights evidence is incomplete.

Current canonical blockers captured by the gate:

- packaged qualification trust policy pin is intentionally unavailable (`None`); no independently reviewed production trust-policy bytes may be invented by this gate;

- first-party Autosport and Nika reuse still has unresolved release/distribution rights records;
- selected external components remain pending exact selected-composition, notice and advisory evidence;
- exact release-composition-to-provenance identity mapping is still absent;
- model/data/news rights and frozen-release SBOM evidence remain absent.

The gate also verifies that the current Python runtime test requirements are exact-version entries and inspects every `PackageReference` under `src/` for exact version identity.

This is not an SBOM and does not approve any dependency. It is a deterministic blocker inventory that prevents a false WP-03 PASS and gives the remaining composition work a machine-checked boundary.

## Resolved in the canonical #2204 lineage

The repository and both .NET qualification workflows select SDK `10.0.100` exactly, with `rollForward=disable`. The isolated research build backend is pinned to `setuptools==84.0.0`. Python workflow selection is exact `3.12.10` and GitHub-hosted runner images are explicit.

The current convergence additionally binds the actual WebView2 lock graph into release provenance, keeps Desktop.Client transitive lock identity synchronized with Desktop, verifies the exact restored `.nupkg` SHA-512 against the lock, binds nuspec id/version plus reviewed license/notice to that exact artifact, and requires locked restore plus restored-rights verification in the same real Actions job under canonical `NUGET_PACKAGES`. These close source-side lock/restored-package-rights gaps. #2204 additionally converges the canonical detached WP-64 supply-chain proof/semantic-subject authority. They do not manufacture external rights, advisory, SBOM, trust-root or release qualification.


## Command modes

`python tools/check_dependency_composition.py` is report mode. It always emits the deterministic blocker inventory and remains usable while WP-03 is intentionally incomplete.

`python tools/check_dependency_composition.py --require-qualified` is the release-enforcement mode. It returns a non-zero exit code whenever any composition blocker remains. Release automation must use this strict form; report mode is not release approval.

## Current reproducibility state

The former mutable Python `3.12` CI blocker is resolved on current main. Baseline, contracts, control-plane, dotnet-foundation, futures, provider-free product, recovery, research, science, Verify AutoTrade and zero-model qualification now resolve Python through the exact patch runtime `3.12.10`; matrix workflows pin `python-version: ["3.12.10"]`. GitHub-hosted OS selection is also explicit for the canonical dual-OS matrices (`ubuntu-22.04` / `windows-2025`).

The remaining WP-03 blockers are evidence/composition boundaries, not mutable Python runtime selection:

- exact final release composition evidence is absent;
- authenticated mapping from distributed release components to provenance component identities is absent;
- model/data/news rights evidence is absent;
- first-party Autosport/Nika release-distribution rights chain remains unresolved;
- inspected candidate external components remain release-blocked until exact composition/notice/advisory evidence exists;
- exact release dependency advisory evidence is absent;
- the generated release dependency manifest must stay synchronized with every package-bearing source project and committed lock graph.

These blockers remain fail-closed and must not be converted into APPROVED/PASS without the corresponding evidence.
