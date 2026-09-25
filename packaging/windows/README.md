# Windows packaging foundation

This directory documents the release-packaging boundary. It is **not** evidence that AutoTrade has a qualified or signed Windows release.

`tools/build_windows_bundle.py` creates a byte-deterministic ZIP from an already built staging directory. Entries are sorted, timestamps and permissions are fixed, compression is disabled to avoid zlib-version drift, every payload file is SHA-256 listed, the archive is bound to an exact 40-character source SHA, and a sibling SHA-256 file is written.

Two modes exist:

- `diagnostics` may package an unqualified build for inspection. The embedded manifest preserves provenance blockers and explicitly grants no trading authority. An optional composition manifest is validated and bound when supplied.
- `release` refuses to run unless the machine provenance manifest says `release_eligible: true` **and** an exact Windows composition manifest is supplied with `--composition`.

## Exact composition gate

The release composition manifest is the allow-list for the delivered staging tree. It binds:

- exact Git source SHA;
- dependency-lock SHA-256 and SBOM SHA-256;
- supported schema minimum/maximum;
- architecture, runtime identifier and minimum Windows version;
- every staged component path, kind, version and SHA-256.

Release packaging fails closed if a staged file is undeclared, a declared file is absent, any component digest differs, the source SHA differs, component identities/paths collide, or required composition fields are missing. The validated composition and the SHA-256 of its exact input bytes are embedded into `bundle-manifest.json`.

This gate does not establish signer authenticity. Signing/trust remains the WP-64 qualification boundary, and a release artifact is not terminal WP-50 evidence until the delivered signed installer is exercised on a clean supported Windows 11 machine.

Secret-like files, private-key formats and symlinks are rejected before archive creation. The bundle itself never grants live-trading authority.

Remaining WP-50 work includes final runtime/host composition after dependent packages converge, one pinned installer/update technology, signer/trust evidence, migration and rollback orchestration, clean-Windows install/update/rollback/uninstall evidence, keyboard/NVDA installer qualification, and final supply-chain evidence.
