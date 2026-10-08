# Plan 5 / Section 6 — Nonexecuting Windows installer fixture

This is **not** a production MSI, MSIX, signed installer, auto-updater, Host
launcher, or evidence of real Windows 11/NVDA acceptance.

## Canonical authorities reused

- `tools/build_windows_bundle.py`: one deterministic ZIP builder.
- `tools/build_windows_install_manifest.py`: one release bundle, manifest,
  source-SHA, SBOM, dependency-lock and every payload-byte verifier. The
  assembly holds that verified bundle descriptor while extracting it.
- `research/autotrade_research/artifacts/durable_publish.py`: one
  cross-process publication lock and atomic selection metadata writer.
- `mvp/autotrade_mvp/windows_update.py`: one signed release plan, journal
  migration, backup, sender-fence, restart/UNKNOWN and rollback authority.
  **The fixture cannot call, replace or override that authority.**

The fixture deliberately cannot run executable files, start a financial Host,
move user data, grant trading permission, or claim a signed release. It only
rehearses an isolated per-user versioned *file assembly*.

## Run with Windows 11 and keyboard/NVDA

From the repository terminal, after building a **synthetic non-secret release
fixture** using the existing canonical tests:

1. Choose a directory with an exact final name `autotrade-fixture-only` that
   is outside your real AutoTrade profile, for example
   `C:\Temp\autotrade-fixture-only`.
2. Execute:
   `python -m tools.windows_fixture_install_assembly --root C:\Temp\autotrade-fixture-only --fixture-only install --bundle C:\Temp\test-release.zip`
3. The response is single-line JSON with `disposition`,
   `version_directory`, `host_started:false`, `trading_authority_granted:false`,
   and `release_qualified:false`.
4. To test updating, run the same install command with a *different, verified*
   synthetic bundle. Old versioned files stay available for rollback.
5. To select a previously verified version (not resume or activate any Host):
   `python -m tools.windows_fixture_install_assembly --root C:\Temp\autotrade-fixture-only --fixture-only rollback --version <content-addressed-version>`
6. To test uninstall:
   `python -m tools.windows_fixture_install_assembly --root C:\Temp\autotrade-fixture-only --fixture-only uninstall`
   Any fixture `state` directory is preserved; a reinstall cannot silently
   erase it. Commands never operate on the real application state location.

The command responds with a text error and nonzero exit code on corrupt
archives, invalid paths, unexpected files, concurrent installation, wrong
source/file hashes, disallowed rollback, old active inventory tampering and
injected staging disk-full failure. No secret or financial data is requested.

## Test automation and failure boundaries

`python -m unittest mvp.tests.test_windows_install_manifest mvp.tests.test_windows_fixture_install_assembly -v`

The suite exercises clean install, side-by-side update, explicit rollback,
uninstall/reinstall preserving state, disk full, changed payload, corrupt
bundle, missing version, malicious selection, symlinks/hardlinks, concurrent
installation and untracked installed files. CI runs it without user/provider
credentials on Ubuntu and Windows-2025 against the **same exact Git SHA**.
Static code alone cannot be counted as successful CI execution.

The fixture deliberately does not provide the actual final signed release,
certification/publisher UI, real Windows installer technology or physical NVDA
acceptance. Those are independently qualified downstream in Plan 9. Plan 5
Section 7 must still evaluate integrated provider-free Web/Desktop/package
source and all available exact-SHA evidence without inventing release truth.
