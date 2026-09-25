# Windows packaging foundation

This directory documents the release-packaging boundary. It is **not** evidence that AutoTrade has a qualified or signed Windows release.

`tools/build_windows_bundle.py` creates a byte-deterministic ZIP from an already built staging directory. Entries are sorted, timestamps and permissions are fixed, compression is disabled to avoid zlib-version drift, every payload file is SHA-256 listed, the archive is bound to an exact 40-character source SHA, and a sibling SHA-256 file is written.

Two modes exist:

- `diagnostics` may package an unqualified build for inspection. The embedded manifest preserves provenance blockers and explicitly grants no trading authority.
- `release` refuses to run unless the machine provenance manifest says `release_eligible: true`.

Secret-like files, private-key formats and symlinks are rejected before archive creation. The bundle itself never grants live-trading authority.

Remaining WP-50 work includes exact runtime composition, installer/update technology qualification, signing identities, prerequisites, migration and rollback orchestration, clean-Windows install/uninstall evidence, keyboard/NVDA installer qualification, and final supply-chain evidence.
