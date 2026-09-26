# Windows packaging foundation

This directory documents the release-packaging boundary. It is **not** evidence that AutoTrade has a qualified or signed Windows release.

`tools/build_windows_bundle.py` creates a byte-deterministic ZIP from an already built staging directory. Entries are sorted, timestamps and permissions are fixed, compression is disabled to avoid zlib-version drift, every payload file is SHA-256 listed, the archive is bound to an exact 40-character source SHA, and a sibling SHA-256 file is written. Bundle and SHA-sidecar publication reuse the canonical `research.autotrade_research.artifacts.durable_publish` boundary: cross-process path locks, final-destination symlink/special-file/hardlink rejection, random same-directory temporary files, file flush/fsync, atomic replacement and parent-directory synchronization where the platform supports it. Safe stale fixed `.tmp` files from the older implementation are removed; unsafe legacy aliases are never followed and are otherwise ignored because publication no longer uses those predictable names. Output and hash destinations are also rejected when they alias the release provenance or composition inputs, preventing the builder from destroying evidence it just consumed.

Two modes exist:

- `diagnostics` may package an unqualified build for inspection. The embedded manifest preserves provenance blockers and explicitly grants no trading authority. An optional composition manifest is validated and bound when supplied.
- `release` refuses to run unless the machine provenance manifest says `release_eligible: true` **and** an exact Windows composition manifest is supplied with `--composition`.

## Exact composition gate

The release composition manifest is the allow-list for the delivered staging tree. It binds the exact Git source SHA, dependency-lock SHA-256, SBOM SHA-256, supported schema range, architecture/runtime identifier/minimum Windows version, and every staged component path/kind/version/SHA-256.

Release packaging fails closed if a staged file is undeclared, a declared file is absent, any component digest differs, source SHA differs, component identities/paths collide, required fields are missing, the runtime identifier contradicts the architecture, or the staged payload does not contain exactly one declared dependency-lock component and one declared SBOM component matching their top-level digests. The validated normalized composition and the SHA-256 of its exact input bytes are embedded in `bundle-manifest.json`.

Secret-like files, private-key formats and symlinks are rejected before archive creation. The bundle itself never grants live-trading authority. Composition integrity also does not establish signer authenticity; signing/trust remains the WP-64/shared qualification-attestation boundary.

## Update and rollback plan

`mvp/autotrade_mvp/windows_update.py` adds a deterministic fail-closed update/rollback planning boundary. It consumes frozen release-candidate manifests, verified pre-update backup evidence and exact journal-schema transition evidence. A plan is blocked on backup mismatch, unresolved migration evidence, forged release metadata or missing reconciliation semantics. Even a ready plan starts the candidate in degraded/no-trading-authority mode and requires post-update or post-restore reconciliation plus separate authority reacquisition.

The canonical update prefix is deliberately fail-closed: verify the candidate signature/hash first, quiesce new admissions, surface and reconcile in-flight/UNKNOWN provider sends, verify the pre-update backup, and only then stop/fence the old financial sender before installing candidate bytes. After the candidate or restored runtime starts degraded, the plan separately requires journal/storage/clock/security identity validation, re-establishment of authenticated provider sessions, provider/account reconciliation, and host/UI compatibility verification before it may enter the state that is merely ready for separate authority reacquisition. Rollback follows the same post-start barriers after quiescing admissions, reconciling in-flight sends, fencing the sender and restoring the prior qualified state. These are ordered planning/checkpoint requirements; they do not manufacture execution evidence or financial reconciliation truth.

The checkpoint/execution boundary revalidates the nested current/candidate release identity, backup verification/source/schema/reconciliation requirements, migration transition/source/status/rollback compatibility, canonical steps and no-trading-authority invariant. Recomputing a plan digest is not sufficient to bypass those gates.

## Verified installer input

`tools/build_windows_install_manifest.py` is the verified installer-input boundary. It accepts only a release-mode, release-eligible deterministic bundle, re-hashes the complete archive and every payload member, rejects untracked/duplicate/unsafe entries, and emits a deterministic manifest for a future signed installer technology. The manifest records runtime dependency mode, per-user versioned application placement, explicit preservation of durable state on uninstall, and the requirement that update/recovery use the separately verified Windows update plan. It never claims that an MSI/MSIX exists or is signed. Manifest and SHA-256 sidecar publication reuse the same canonical durable-publication boundary, including cross-process locking and final symlink/special-file/hardlink rejection. Safe stale predictable `.tmp` files are compatibility cleanup only; new writes use random same-directory temporaries. The manifest or its digest is also forbidden from aliasing the verified release bundle itself.

Publication creates persistent hidden `.lock` sidecars next to bundle, digest and installer-manifest outputs. They are coordination metadata for cooperating writers, not release payload, provenance, signature or user data. Release upload/install manifests must enumerate explicit deliverables and must never glob these lock files into a shipped artifact.

## Remaining terminal WP-50 work

This lineage is still not a qualified installer or updater. Terminal WP-50 still requires the final converged runtime/host composition; one selected and pinned installer/update technology; generation and qualified signing of installer and executable artifacts; prerequisites; executable migration/update/rollback orchestration through WP-48/WP-49; clean Windows 11 install/update/interrupted-update/rollback/uninstall matrices; split-brain fencing; durable-state preserve/delete-choice semantics; final supply-chain trust; and real keyboard/NVDA evidence bound to the delivered signed artifact digest and exact internal source SHA.

WP-50 must consume a release candidate accepted by the canonical release/trust authority; it must not self-authorize a structurally plausible FROZEN manifest. Until those gates pass, these artifacts remain implementation foundations rather than release qualification.
