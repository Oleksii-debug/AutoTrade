# One provider-free AutoTrade candidate

This is an unsigned diagnostics candidate, not a qualified Windows release. It
reuses the existing WPF application, authenticated host, immutable web bundle,
simulator, financial journal, backup and packaging boundaries. It contains no
provider adapter configuration, API keys or real-order path.

## Build and run

The `provider-free-product` workflow checks out the exact PR head and publishes
**two distinct self-contained win-x64 executables from that same SHA/toolchain**:
`AutoTrade.Desktop.exe` and `AutoTrade.Host.exe`. Each publish gets independent
exact-head CI evidence. Immediately after that evidence is created, the workflow
binds it to the SHA-256/byte identity of the primary executable and to a canonical
digest of every other byte in that publish payload (excluding the evidence file
itself to avoid a self-referential hash). Candidate assembly captures each publish
once as a held snapshot and requires the held bytes to reproduce both bindings.
A replacement of the executable, runtime/config files or other publish payload
between evidence creation and candidate capture therefore fails closed instead of
being relabelled as the evidenced exact-head build. The builder also refuses a
missing Host, stale build evidence, or a source-SHA mismatch and records each
executable's SHA-256 and byte length in the dependency lock/candidate result. Host
files live under `payload/host/` so the two self-contained .NET publishes cannot
overwrite each other's runtime files.

The packaged C# Host is deliberately fail-closed today. Its default authority is
`BLOCKED`; without the canonical journal/risk/provider execution authority it
must exit with code 78 and the `AUTOTRADE_HOST_STARTUP_BLOCKED` marker **before
binding a listener**. The Windows candidate workflow executes that exact packaged
binary and requires this behavior. Shipping the Host binary therefore does not
mint PAPER/LIVE or provider-send authority and does not replace the current
provider-free ZERO simulation host owned by the Desktop runtime.

The workflow also downloads hash-pinned Python 3.12.10 and WebView2 SDK inputs
and calls the existing source-staging and bundle builders through
`tools/build_provider_free_candidate.py`. Desktop NuGet restore uses committed
package locks; the Host project adds no third-party package dependency beyond the
shared first-party contracts/framework. The release provenance gate stays blocked.

The archive has `bundle-manifest.json` and `payload/`. Extract the entire archive
to a regular local directory. Run `payload/AutoTrade.Desktop.exe`. Preserve the
outer inventory: startup verifies its source marker and every declared file
before launching the owned provider-free runtime. Missing, changed, extra or
reparse-aliased files cause a textual startup error. An unsigned hash inventory
is an integrity check, not a trusted signature.

The desktop uses its bundled isolated Python interpreter; no Python, .NET SDK,
developer checkout or environment variables are required on the target machine.
The **Microsoft Edge WebView2 Evergreen Runtime is a prerequisite**. It is not
silently downloaded or elevated by AutoTrade. If absent, native status and
emergency controls remain available and the Web interface reports unavailability.
Actual installed behavior still needs Windows qualification.

Data and the WebView profile are in `%LOCALAPPDATA%/AutoTrade-ZERO`, outside the
installation. The provider-free ZERO host binds an exclusive loopback listener
on an ephemeral port, then gives the shell one-use pairing material through its
owned pipe. Closing the window requests production-host drain; abrupt parent death
stops that host and its worker, leaving canonical recovery to the next launch.
The separately packaged C# Host remains a blocked product-process prerequisite
until the canonical financial/provider authority is composed into it.

## Automated whole scenario

`mvp/tests/test_provider_free_product.py` tests the real authenticated HTTP
surface and an actual child-process exit after the first atomic half-fill.
`tests/Product/provider-free-browser.cjs` repeats the lifecycle in Chromium with
Tab, Enter and select-key navigation. It checks semantic status/alert regions,
research results, partial-fill state, recovery, cash, agent decisions, replay,
backup, restore and the preserved reconciliation gate. No mouse action is used.

The Windows CI job repeats the browser scenario against the **packaged isolated
Python and packaged source**, and independently exercises the exact packaged
`AutoTrade.Host.exe` fail-closed startup contract. It does not constitute a WPF
UI Automation or NVDA test. Results must be read from the exact source SHA's
completed Actions artifacts; adding a workflow is not a passing result.

## Sections 30–37 qualification boundary

| Section | Implemented or tested | Still required for closure |
| --- | --- | --- |
| 30 Web | One host-backed semantic UI; simulation/recovery/backup and safety commands; real browser lifecycle | Installed Windows browser evidence and operator acceptance |
| 31 Desktop | Existing WPF shell owns ZERO host, authenticates, embeds the same UI with WebView2; separately packaged C# Host is fail-closed before bind | Compose canonical financial/provider authority into C# Host; execute compiled shell on Windows and verify shutdown/fallback |
| 32 Accessibility | Named controls, semantic tables, labels, live regions, keyboard browser scenario | Real Windows 11 + NVDA test, focus transitions into/out of WebView2 |
| 33 Packaging | Separate self-contained Desktop+Host publishes from one SHA, payload-bound publish evidence, isolated embedded Python, exact Git source staging, archive hashes, installed-file preflight, packaged Host exit-78 oracle | Completed Windows packaged run, clean standard-user install, WebView2-present/absent cases |
| 34 Update | Existing backup, schema migration and signed update/rollback planning retained | Installed updater transaction, interrupted update and rollback on this exact package; no automatic upgrade is shipped |
| 35 Qualification/freeze | Source SHA, file hashes, locks, payload-bound publish provenance and exact-head CI artifacts; existing signed qualification gates retained | Real evidence, owner/trust signatures, rights/advisory review and final frozen package |
| 36 Load | 120-observation resume equivalence; bounded HTTP capacity, explicit overload rejection and same-identity retry | Sustained market ingress, slow physical disk, large journals, concurrent research and target-machine latency/memory qualification |
| 37 Whole ZERO | Actual process crash after partial booking, retained observation recovery without resend, canonical accounting, synthetic settlement, backup/restore and real keyboard browser state inspection | Successful packaged Windows execution and final operator-ready qualification |

## Windows 11 and NVDA evidence to record

Record exact source SHA, package SHA-256, Windows build, WebView2 version, NVDA
version, operator, UTC times and actual result for every step. Never populate a
PASS by copying this procedure or synthetic fixtures.

1. Extract the complete package as a standard user to a path with spaces.
2. Start the executable without developer software. Read the active account,
   SIMULATION environment, freshness and native status with NVDA.
3. Use only the keyboard to enter and leave WebView2, reach every essential
   command, inspect portfolio/orders/fills/research and hear dynamic results.
4. Run the frozen simulation. Confirm three terminal orders, six execution
   slices, cash 895.696 USD and position one. Read all errors as text.
5. Restart after an abrupt process stop; recover the same durable work and
   confirm no duplicated fills or accounting changes.
6. Create a backup, restore to a fresh data directory, inspect and recover the
   restored session. Confirm the real-trading reconciliation gate remains.
7. Repeat startup with a required file removed/changed, a denied data directory,
   a second instance, missing WebView2 and a stale session. Record truthful
   failure announcements and keyboard recovery.
8. Close during accepted work and confirm drain or explicit recovery after a
   forced timeout. Run update/rollback qualification only after an actual
   signed updater transaction is integrated.

No real-paper/live execution or profitability qualification is implied.
