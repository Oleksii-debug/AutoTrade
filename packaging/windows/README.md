# Windows packaging foundation

This directory documents the release-packaging boundary. It is **not** evidence that AutoTrade has a qualified or signed Windows release.

`tools/build_windows_bundle.py` creates a byte-deterministic ZIP from an already built staging directory. Entries are sorted, timestamps and permissions are fixed, compression is disabled to avoid zlib-version drift, every payload file is SHA-256 listed, the archive is bound to an exact 40-character source SHA, and a sibling SHA-256 file is written.

Two modes exist:

- `diagnostics` may package an unqualified build for inspection. The embedded manifest preserves provenance blockers and explicitly grants no trading authority.
- `release` refuses to run unless the machine provenance manifest says `release_eligible: true`.

Secret-like files, private-key formats and symlinks are rejected before archive creation. The bundle itself never grants live-trading authority.

Remaining WP-50 work includes exact runtime composition, installer/update technology qualification, signing identities, prerequisites, migration and rollback orchestration, clean-Windows install/uninstall evidence, keyboard/NVDA installer qualification, and final supply-chain evidence.

`mvp/autotrade_mvp/windows_update.py` adds a deterministic fail-closed update/rollback planning boundary. It consumes frozen release-candidate manifests, verified pre-update backup evidence and exact journal-schema transition evidence. A plan is blocked on backup mismatch, unresolved migration evidence, forged release metadata or missing reconciliation semantics. Even a ready plan starts the candidate in degraded/no-trading-authority mode and requires post-update or post-restore reconciliation plus separate authority reacquisition.

This is still not a qualified installer or updater. Remaining WP-50 work includes an actual signed installer/update technology, execution of the plan on clean Windows, interrupted-update recovery, migration tooling, uninstall/data-retention behavior, real keyboard/NVDA installer evidence, signing identities and final supply-chain qualification.

`tools/build_windows_install_manifest.py` is the verified installer-input boundary. It accepts only a release-mode, release-eligible deterministic bundle, re-hashes the complete archive and every payload member, rejects untracked/duplicate/unsafe entries, and emits a deterministic manifest for a future signed installer technology. The manifest records runtime dependency mode, per-user versioned application placement, explicit preservation of durable state on uninstall, and the requirement that update/recovery use the separately verified Windows update plan. It never claims that an MSI/MSIX exists or is signed.

Remaining WP-50 work is deliberately explicit: select and pin a Windows installer technology; generate the actual installer from this verified inventory; sign installer and executable artifacts with qualified identities; run clean-install/update/interrupted-update/rollback/uninstall matrices on supported Windows targets; prove prerequisite handling; prove explicit data-deletion choice; exercise the full update checkpoint/recovery flow against real disk/process state; and produce real NVDA keyboard evidence. Until those gates pass, these artifacts remain implementation foundations rather than release qualification.
