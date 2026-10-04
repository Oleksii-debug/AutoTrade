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

## Remaining reproducibility blocker

Five Python CI workflows still select the mutable minor line `3.12` rather than one qualified cross-platform patch runtime. The audit reports each as `NON_EXACT_CI_PYTHON_VERSION`. This is intentionally unresolved until a Windows/Linux-compatible exact runtime is selected and qualification evidence exists; the gate must not invent a portable patch pin.
