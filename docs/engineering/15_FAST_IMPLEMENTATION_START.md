# AutoTrade — shortest path from the verified bootstrap

Baseline: 2026-09-22. This is an execution guide for the existing 65 work packages, not a replacement roadmap. Start at `control/INDEX.json`. Reuse the existing schemas, research primitives, registry tests and provenance records; do not recreate the repository or repeat the completed source inventory.

## 1. Work that immediately shortens delivery

| Priority / package | Concrete next output | Reuse and boundary | Acceptance before downstream reliance |
|---|---|---|---|
| WP-01 contracts | One versioned positive/negative corpus used by C#, Python and TypeScript; generated or explicitly reviewed bindings | Existing 12 JSON Schema files and OpenAPI; generate DTOs and clients, not financial decisions | Same corpus verdicts in all languages; decimal strings never become binary floating money; unknown command fields and invalid versions rejected |
| WP-03 exact composition | Selected SDK/package graph, transitive locks, notices, SBOM | `provenance/components.json`; distinguish inspected commit from published package | Reproducible restore and explicit first-party distribution-rights record; no invented license |
| WP-02 LEAN adoption | Small embedding/build harness at the reviewed commit | Commit `985ef30ad3ac774218c5ac516b4cb0aa2655730f`; one OMS and economic owner | Actual Windows/Linux build, callbacks, decimal conversion, shutdown/restart and guarded brokerage seam |
| WP-05/06/14/15/17/18/19/20 | Durable intent → reservation → admission → outbound attempt → fill → ledger → reconciliation slice | SQLite transaction plus LEAN integration; independent economic fixtures | Restart at each boundary cannot invent fills, duplicate exposure or release UNKNOWN reservations |
| WP-08/21 | Deterministic provider contract harness with synthetic responses, request counts and stream sequence control | Existing provider schemas; .NET TimeProvider testing where the code accepts it | Distinguish ack, fill, missing data, timeout and reconciliation coverage; expiry/revocation while waiting for quota blocks actual send |
| WP-24 | WhiteBIT SDK qualification record and thin adapter | WhiteBit.Net + compatible CryptoExchange.Net graph | Explicit environment, stable client ID, per-attempt authority, mutation retry policy, redacted logging, spot/collateral units and histories |
| WP-27 | Alpaca route decision and adapter fixtures | Existing LEAN route versus official SDK stable 7.2.2; choose one sender | Stable-package source/dependencies verified separately from inspected 8.x beta main; incomplete financial fields cannot silently become zero |
| WP-43/44/45/53 | Accessible UI over the same simulator-backed state | Versioned host API; semantic web UI and WPF native emergency/status | NVDA/keyboard scripts on Windows; accepted command is not reported as completed execution |
| WP-51 / development control | Repeatable local checks, baseline export, eventual qualified registry service | Existing `control/tools`, tests and `tools/baseline.py` | Exact source evidence; remote CAS, permissions and cross-platform results before concurrent mutation |

WP-05 and WP-06 identifiers retain their canonical bank definitions. This table identifies the integration path; the bank owns exact dependencies and scopes. Exploratory build/fixture work may proceed before an entire dependent package is DONE, but integrated completion requires accepted prerequisite artifacts.

## 2. Reuse choices to apply now

Use WhiteBit.Net before writing a WhiteBIT transport from scratch. Retain the already reviewed LEAN brokerage route for each other provider where it meets the actual product/account requirement. SDK candidates do not prove complete order semantics, account entitlement, test-environment equivalence, or real execution. All six providers stay in scope regardless of Ukraine/Slovakia registration assumptions.

Use a single fixture vocabulary across adapters: source package/revision, environment, request/response redaction version, intended effect, actual outbound count, unique execution identities, query coverage, stream sequence, observed times, expected economic postings and evidence class. Provider-specific cases extend that vocabulary. Avoid six independently invented testing frameworks.

AutoTrade owns the actual send boundary. SDK rate-limit queues, retries and HTTP resilience handlers must not create an unrecorded send after revocation or expiry. Microsoft documents `DisableForUnsafeHttpMethods` for POST/PATCH/PUT/DELETE/CONNECT; this is a candidate configuration aid, not proof that another SDK has no internal retries. Audit every installed layer and classify operations by financial effect. See [official API reference](https://learn.microsoft.com/en-us/dotnet/api/microsoft.extensions.http.resilience.httpretrystrategyoptionsextensions.disableforunsafehttpmethods?view=net-10.0-pp).

Use [FakeTimeProvider](https://learn.microsoft.com/en-us/dotnet/core/extensions/timeprovider-testing) to advance injected .NET clocks deterministically. Keep market/replay time separate from live authority expiry and monotonic elapsed time. A SDK that directly reads system time is outside that injected clock; qualify or wrap its seam. Do not speed up tests by removing deadlines or sleeping against wall time.

For WP-50, evaluate [Velopack](https://docs.velopack.io/integrating/overview) as an installer/updater candidate before writing that machinery. Its documented [specific-version/downgrade support](https://docs.velopack.io/integrating/specific-version) handles application version selection. AutoTrade must separately quiesce sending, reconcile, back up durable state, verify DB compatibility and preserve emergency access. This is documentation-level screening here; exact package/license/dependency and NVDA installation checks remain open. Application downgrade is not a database rollback.

## 3. Reduce coordination work

1. Select one bounded package responsibility and accepted input revision. Reuse its existing files and evidence. Avoid a second implementation branch for the same scope.
2. In the current single-writer bootstrap, implement the next concrete artifact without waiting for a fully automated swarm. Read-only research remains independent. Later concurrent mutation requires the trusted registry service and protected branch path.
3. Keep DTO generation, SDK qualification, independent financial oracles and UI semantics as separate responsibilities. A contract migration names affected consumers and updates the common corpus once.
4. Integrate the simulator slice early. Continue provider research and UI fixtures without waiting for all six external connections. Never claim a simulator pass as broker or profitability evidence.
5. Prefer dependency updates with a measured failing fixture and a narrow change. New frameworks require a concrete missing capability and total adoption/maintenance cost comparison.
6. Record a reproducible command, source SHA, fixture versions, result and remaining limit with each handoff. Do not infer DONE from file count or test count.

## 4. Commands that future workers can reuse

Run from the repository root with Python 3.12 and Git installed:

```text
python -m pip install -r requirements-dev.txt
python tools/verify.py
python tools/baseline.py refresh
python tools/baseline.py check
```

`verify.py` runs the baseline, contract, control and research suites without live credentials. The .NET build is a separate gate; Windows/NVDA is a separate qualification campaign.

After changes are committed and registry refs fetched:

```text
git fetch origin main control/registry
python tools/baseline.py pack --ref HEAD --registry-ref origin/control/registry
python tools/baseline.py verify --archive <generated-zip>
```

The export reads committed Git objects, includes documents 00–16, the complete repository source/product snapshot, the operational registry snapshot, accessible HTML and SHA-256 manifest. It excludes its own manifest from hashing; the outer ZIP hash is separate. The registry snapshot is historical evidence, never live ownership authority.

## 5. What remains outside this bootstrap

Full financial runtime, LEAN embedding, generated cross-language bindings, provider qualification, model/science implementation, installer signing and real NVDA acceptance are implementation work. The bootstrap supplies verified starting material and repeatable checks. It does not certify live trading, release readiness or economic edge.
