# WP-03 dependency composition gate

Status: **IN PROGRESS / FAIL CLOSED**.

This gate audits the checked source tree and deliberately refuses to call the release dependency composition qualified while exact dependency or rights evidence is incomplete.

Current exact-main blockers captured by the gate:

- first-party Autosport and Nika reuse still has unresolved release/distribution rights records;
- selected external components remain pending exact package/transitive composition and notice review.

The gate verifies exact, hash-qualified Python dependency boundaries for development requirements, the repository-root build backend, and the research runtime dependency on the repository-root package. It inspects every `PackageReference` under `src/` for exact version identity, rejects path-unsafe NuGet identities, and qualifies restored NuGet package bytes against the lock hash, package identity, license and notice records.

This is not an SBOM and does not approve any dependency. It is a deterministic blocker inventory that prevents a false WP-03 PASS and gives the remaining composition work a machine-checked boundary.

## Resolved in this lineage

The repository and .NET qualification workflows select SDK `10.0.100` exactly, with `rollForward=disable`. The root and research build backends are pinned to `setuptools==84.0.0`, and the research runtime dependency is bound exactly to `autotrade-exact-numeric==0.0.1`.

Every CI installation of `requirements-dev.txt` must use the one fail-closed hash-only command with `--require-hashes --no-deps --only-binary=:all:`. External GitHub Actions are required to use immutable 40-hex commit pins; container actions, if added, require an immutable `sha256` digest. Those CI action identities and the Git blob identities of the workflows that invoke them are recorded in the release dependency manifest.

These remove SDK feature-band, Python build/backend, mutable workflow-action and requirements-install drift from WP-03 evidence; they do not resolve release-composition, first-party distribution-rights, model/data-rights or authenticated advisory-review blockers.


## Command modes

`python tools/check_dependency_composition.py` is report mode. It always emits the deterministic blocker inventory and remains usable while WP-03 is intentionally incomplete.

`python tools/check_dependency_composition.py --require-qualified` is the release-enforcement mode. It returns a non-zero exit code whenever any composition blocker remains. Release automation must use this strict form; report mode is not release approval.

## Reproducibility closure on current main

All inspected Python CI setup points now select exact Python `3.12.10`, including matrix-driven workflows. The composition gate fails closed on unresolved expression-valued `python-version` authority instead of trusting an unevaluated expression. The .NET SDK remains exactly `10.0.100` with literal `rollForward: "disable"`.

NuGet lock qualification remains conditional: the current `src/` tree has no `PackageReference` dependency, so no synthetic `packages.lock.json` is invented. If a package-bearing release project appears, the gate requires its sibling lock, validates Direct and Transitive records plus canonical SHA-512 content hashes and dependency edges, requires inspectable per-project locked restore coverage, and binds that locked graph into release provenance.

## Restored NuGet byte authority

For any package-bearing release project, a matching lock record and rights record are necessary but not sufficient. The restored verifier now also requires:

- a path-safe package name and version that cannot escape the configured NuGet package root;
- no symlink traversal through the package identity or reviewed package evidence files;
- exactly one restored `.nupkg` and exactly one `.nupkg.sha512` authority;
- the SHA-512 digest of the actual `.nupkg` bytes to equal the lock-file `contentHash`, not merely the text stored in the sidecar;
- exactly one `.nuspec` whose metadata `id` and `version` equal the locked package identity;
- the declared license file and notice file to be regular files in the exact package directory;
- the restored license text to equal the reviewed repository evidence.

## Remaining terminal blockers

The checked manifest remains deliberately `release_eligible=false`. Terminal WP-03 closure still requires evidence that is not present in the repository:

- exact authenticated release composition for the release source SHA;
- model/data/news use and redistribution-rights evidence for the actual release scope;
- a resolved release/distribution-rights chain for first-party reuse that is actually distributed, including the current Autosport migration;
- exact dependency/advisory review bound to the generated dependency graph and accepted through the canonical independent qualification trust boundary;
- release-distribution qualification for any component that is actually part of the shipped composition.

A candidate-authored JSON document cannot mint terminal trust. The advisory validator intentionally remains fail-closed with `authenticated_trust_required` after structural validation until the separately controlled signed qualification boundary accepts the same evidence.
