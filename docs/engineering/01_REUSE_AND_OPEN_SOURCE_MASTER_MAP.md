# AutoTrade — reuse, evidence and migration map

Baseline 2026-09-22. Decision owner: engineering architecture. This document owns reuse choices; document 02 owns the destination contracts. No source code was copied or imported during this task.

## 1. Evidence discipline

The audit read actual source and selected tests, not only READMEs. It is a targeted architectural audit, not an exhaustive correctness certification. No upstream suite was executed and no performance benchmark was run. GitHub source access became rate-limited during the initial audit, but follow-up research completed the previously missing read of `tests/test_replay_jsonl_integrity.py` and the full Nika model-gateway contracts. Document 14 records the additional exact evidence and narrowly supersedes the earlier uncertainty.

First-party snapshots:

- [Autosport cb102d85f0c820c7097875191deca73e53ec94f5](https://github.com/Oleksii-debug/Autosport/tree/cb102d85f0c820c7097875191deca73e53ec94f5), commit observed 2026-09-21.
- [Nika-Core 2f7be3389109d7dd6fb3bae40540fe0cf2eba695](https://github.com/Oleksii-debug/Nika-Core/tree/2f7be3389109d7dd6fb3bae40540fe0cf2eba695), commit observed 2026-09-15.
- [LEAN repository](https://github.com/QuantConnect/Lean): follow-up verification established commit `985ef30ad3ac774218c5ac516b4cb0aa2655730f` (2026-09-18), tree `4b163abf9fca60e731b76510b9ae6721ffff7e6c`. Keep the exact commit identity in build provenance and review later deltas before updating.

Newer commits do not invalidate this snapshot audit, but imports must compare the chosen new revision against it. At the inspected first-party snapshots, no root LICENSE/COPYING/NOTICE was found and the inspected pyproject files did not establish a reuse license. User ownership is useful context, not proof of rights to every contribution or dependency. A/B below are technical classifications; import requires a recorded owner/contributor rights basis and retained notices. Do not describe either repository as permissively licensed without that evidence.

## 2. A/B migration dossiers

### A1. Strict JSON ingestion — direct reuse candidate

- Source: Autosport `src/autosport/json_integrity.py`.
- Symbols inspected: `strict_json_loads`, `DuplicateJsonKeyError`, `NonStandardJsonConstantError`, `InvalidJsonDomainError`, `jsonl_bytes_are_blank`.
- Dependencies: standard-library JSON, math and typing; no trading or sports dependency in the inspected implementation.
- Existing behavior: duplicate-key rejection; non-standard NaN/Infinity rejection; domain checks including invalid scalar/UTF-8 cases and oversized integer protection. Finite binary floats are accepted; that does not make them suitable for money.
- Relevant tests: follow-up research read `tests/test_replay_jsonl_integrity.py` at the audited Autosport commit. It explicitly covers duplicate top-level/nested keys, nonstandard constants, overflowing numbers, >640-digit integers, invalid UTF-8, lone surrogates, non-object records, physical line reporting and unreadable paths. The AutoTrade migration still requires its own neutral characterization suite.
- Destination: `research/autotrade_research/io/strict_json.py`, only for research/import boundaries. .NET contracts use their own strict serializer configuration and common language-neutral fixtures.
- Changes: preserve the neutral parser behavior and notices; add payload-byte and nesting limits at the caller; reject numeric JSON values for financial fields at schema validation. Do not turn this parser into a money library.
- Risk: error classes and exact accepted scalar domain become an API; importing only the main function without its checks would weaken it.
- Acceptance: cross-language fixture corpus rejects duplicate keys at every nesting level, NaN, infinities, forbidden money numbers, overlong/deep payloads and invalid encodings; valid Unicode and canonical decimal strings round-trip. The Python component imports without Autosport installed.

### B1. Durable artifact publication — extract neutral subset

- Source: Autosport `src/autosport/integrity.py`.
- Symbols inspected: `sha256_file`, `ensure_durable_file`, `durable_path_lock`, `atomic_write_json`.
- Relevant tests read: `tests/test_durable_path_lock.py`, `tests/test_integrity_atomic_write.py`.
- Dependencies: filesystem, hashing, JSON, OS durability/locking support. Hidden coupling: `atomic_write_json` recognizes scientific-registry shapes and imports `MonotonicWorkspaceAuthority`; therefore the whole module is NOT directly reusable.
- Destination: `research/autotrade_research/artifacts/durable_publish.py`.
- Changes: extract hash, same-directory temporary publication, flush/fsync and lock mechanics into a neutral component; remove registry-shape dispatch; put science monotonicity in the science repository transaction. Specify Windows rename/share behavior and POSIX directory durability separately.
- Risk: rename atomicity is not transactionality across files; a successfully written model without its committed manifest is an orphan, not a promoted model. A hash chain is not tamper proof against an administrator who can replace the root.
- Acceptance: crash before/after write, sync and rename yields old artifact, new verified artifact or quarantined orphan; no partial artifact becomes reachable by a committed manifest. Locked-path aliases, permission failures and close failures are tested on Windows and Linux. This helper must never write financial ledger state.

### B2. Local single-host lock — limited reuse

- Source: Autosport `src/autosport/workspace_lock.py`; inspected class `WorkspaceEconomicLock` and its acquire/release lifecycle.
- Relevant tests inspected: `tests/test_workspace_economic_lock.py` (selected implementations and test inventory), plus durable-path-lock tests.
- Dependencies: OS advisory locks (`fcntl`/`msvcrt` paths), filesystem identity and process lifetime.
- Coupling: workspace/economic naming, lock-path assumptions, poisoning behavior after failed release. No distributed fencing guarantee.
- Destination: research-worker exclusive artifact publication lock. The C# execution host uses an independently implemented account-owner fence and the same behavioral fixtures, not a Python runtime dependency.
- Changes: neutral resource key, documented local-filesystem requirement, reject path aliases and mutable identity, explicit poisoned state. Never use this lock to authorize remote host takeover.
- Acceptance: two processes contend on one canonical resource; aliases cannot bypass exclusion; process death releases local OS ownership; failed cleanup cannot incorrectly report successful release; network-share usage is rejected. No financial order can be enabled by merely acquiring this file lock.

## 3. C/D source audit

| Source and inspection depth | Classification | Finding and AutoTrade destination |
|---|---|---|
| Autosport `replay.py`, full; `tests/test_replay_firewall_reuse.py`, full | C | Ordering and visible-time firewall are valuable. Full event list/future settlement data live in the same process; this is not a hostile-code sandbox. Rebuild causal views around LEAN data injection and isolated workers. |
| `providers.py`, full | C contracts / D market implementation | Provider abstraction useful; sports odds constraints such as values above one are invalid for prices. New instrument/data contracts in document 02. |
| `portfolio.py`, selected substantial implementation | C invariants / D ledger | PaperTicket win/loss/void settlement does not model inventories, margin, funding, FX or option assignment. Reuse conservation scenarios, use LEAN plus AutoTrade accounting projection. |
| `real_execution_ledger.py`, action/plan, attempt, retry and reconciliation sections | C | UNKNOWN and externally proven absence before retry are useful. Bookmaker stake/odds actions are not securities order state machines. New journal and execution event contracts. |
| `model_compute_router.py`, substantial route implementation | C | Exact compute identity and measured shadow value-of-compute gates are useful. SportDomainFitnessObservation creates coupling; implement the neutral evaluation contract. |
| `scientific_registry.py`, promotion section and interfaces | C | Protocol fingerprints, holdout-use accounting and evidence-backed promotion inform ScienceRegistry. Do not inherit domain metric-direction assumptions or file store authority. |
| `experiential_learning.py`, imports and symbol inventory only | C provisional | Experience/learning concepts located; insufficient body review for implementation reuse. Requires a focused source read if migration is proposed later. |
| `storage.py`, imports and interfaces only | C provisional | Useful inventory, no direct reuse certification. SQLite authority schema is new and explicit. |
| `windows_gui.py`, accessibility configuration and interface inventory | C | Automation IDs and keyboard scenarios useful; Tk/tk_uia code would conflict with the selected shared web/WPF architecture. |
| `accessibility_audit.py`, interface inventory | C provisional | Test ideas only; does not prove NVDA behavior in the new UI. |
| Nika `model_gateway/gateway.py`, substantial body; `providers.py`, full; `contracts.py`, interface inventory | C now; narrow B possible after complete contract audit | Async provider execution, privacy boundaries, cancellation and redacted failures are useful. Prefer direct HTTP/SDK composition; do not import Nika orchestration wholesale. Exact class extraction is intentionally not authorized by this partial audit. |
| Nika `tests/test_model_gateway_secret_boundaries.py`, `tests/test_ollama_provider.py`, full | C tests | Preserve secret-redaction, provider failure, local endpoint and cancellation scenarios as independent AutoTrade contract tests. Mock echo is not a useful deterministic trading strategy. |
| Nika `memory/service.py`, full | C concepts / D cumulative-memory implementation | Upsert replaces payload; expiry deletes; wall-clock use conflicts with immutable episodes and replay time. Build append-only episode records with derived retrieval indexes. |
| Nika `scheduler/apscheduler_adapter.py`, full | C | Task/audit/state coupling makes wholesale reuse expensive. Use a durable job table with simple scheduled wake-ups; APScheduler is optional infrastructure, not financial authority. |
| Nika `runtime/idempotency.py`, interface inventory | C provisional | No claim that its idempotency model establishes financial exactly-once effects. |

CI, installer and packaging quality were not fully audited. Existing source/test observations are evidence of useful engineering, not proof that either entire product is finished. The selected first-party imports are deliberately small enough that AutoTrade does not depend on an unfinished application's release cycle.

## 4. Whole-platform comparison

Maintenance/release observations are a dated snapshot, not a future version recommendation. Open issue counts include workflow differences and sometimes PRs; they are not a quality score. Public issue reports below are reported risks, not independently reproduced defects.

| Candidate | License / observed release and activity | Fit, evidence and decision |
|---|---|---|
| [QuantConnect/Lean](https://github.com/QuantConnect/Lean) | Apache-2.0; source active September 2026. GitHub Release v2.4.0.1 dates to 2017, so use commit/build provenance rather than interpreting that tag as current engine age. | SELECT foundation. Read `Common/QuantConnect.csproj`, `Common/Interfaces/IBrokerage.cs`, transaction-handler interface sections and equity-fill test inventory. .NET 10 target; Windows/.NET fit. Core multi-asset burden removed. Full embedding/performance test remains required. |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | LGPL-3.0; observed 2.0.0rc5, September 15, 2026; active. | Strong Rust/Python event engine, Windows packages and adapters; ALTERNATE, not second OMS. Release candidate and LGPL distribution obligations increase adoption review. Reported #5047 concerns cash-account fills in bar simulation; test independently. [License guidance](https://nautilustrader.io/legal/open-source-licensing/), [adapter docs](https://nautilustrader.io/docs/latest/concepts/adapters/). |
| [vnpy/vnpy](https://github.com/vnpy/vnpy) | MIT; 4.4.0 May 14, 2026; September activity. | Mature event/gateway ecosystem and Windows support. China-focused integrations/UI and different research boundary add work for this product. Keep as adapter/concept reference, not foundation. |
| [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) | Apache-2.0; 2.16.0 July 29, 2026. | Crypto market-making/connectors useful; not complete traditional multi-asset base. Reported #8094 cancel/fill race informs recovery fixtures. Avoid importing its second portfolio/execution authority. |
| [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) | GPL-3.0; 2026.8 August 31, 2026. | Maintained crypto strategy bot. Asset scope and distribution policy mismatch for selected base; C conceptual tests only. GPL does not prohibit commercial use, but is not equivalent to permissive licensing. |
| [microsoft/qlib](https://github.com/microsoft/qlib) | MIT; 0.9.7 August 2025, continuing September 2026 work. | Optional offline research reference. Config/code execution and serialization attack surface require isolated trusted workflows. PRs #2339/#2340 observed as security-related work; do not ingest arbitrary models/configs. Not live authority. |

LEAN specific adoption risks: #9795 discusses crypto/crypto-future delisting support; existing engine coverage is not proof that all perpetual/inverse/lifecycle semantics match each provider. `IBrokerage` separates request methods from order-event delivery, supporting the required ack/fill distinction. Equity-fill tests include bid/ask fills and stale/pre-submit data behavior; event realism must still be validated for the actual data frequency. The inspected dependency graph includes pythonnet, Newtonsoft.Json, NodaTime, QLNet and MathNet; unused Python execution can remain disabled, but transitive binaries still require inventory and vulnerability review.

## 5. Selected and optional components

| Component / exact repository | Role and adoption | License/status evidence and obligation |
|---|---|---|
| [ccxt/ccxt](https://github.com/ccxt/ccxt) | Optional exchange transport fallback; official native/LEAN adapter preferred where complete. Never use its unified names as proof of identical semantics. | MIT LICENSE.txt inspected; observed 4.5.82 Sep 21, 2026. Preserve notice. Frequent changes require version pins and per-provider recorded fixtures. Reported #30574/#30575 concern market filtering/trigger routing. |
| [online-ml/river](https://github.com/online-ml/river) | Online baseline estimators and drift detectors in isolated learning workers. | BSD-3-Clause metadata; 0.26.1 Aug 21, 2026. Preserve notices; pin and benchmark before adoption. Drift does not authorize promotion. |
| [optuna/optuna](https://github.com/optuna/optuna) | Offline bounded search, trial registry integration. | MIT; 5.0.0 Sep 7, 2026. Trial failures and concurrency limits are experiment facts, not discarded outcomes. |
| [mlflow/mlflow](https://github.com/mlflow/mlflow) | Optional experiment UI/tracking export; not canonical promotion registry. | Apache-2.0; 3.16.1 Sep 17, 2026. [GHSA-gqvg-gmmx-x4hm](https://github.com/mlflow/mlflow/security/advisories/GHSA-gqvg-gmmx-x4hm) describes a serialization-related issue patched in 3.15.0. Pin supported fixed versions; isolate artifacts and authentication. |
| [lballabio/QuantLib](https://github.com/lballabio/QuantLib) | Optional independent derivatives valuation oracle, not provider margin authority. | LICENSE.TXT inspected: BSD-style notice and non-endorsement conditions, multiple retained notices. GitHub NOASSERTION metadata is not the license text. |
| [QuantConnect/Lean.Brokerages.InteractiveBrokers](https://github.com/QuantConnect/Lean.Brokerages.InteractiveBrokers), [Binance](https://github.com/QuantConnect/Lean.Brokerages.Binance), [Kraken](https://github.com/QuantConnect/Lean.Brokerages.Kraken) | Preferred adapter audit starting points; wrap guarded boundary and add missing contract coverage. | Apache-2.0 repository metadata observed, September activity. Underlying SDK/API terms are separate. No claim of full feature coverage from repository name. |
| [QuantConnect/Lean.Brokerages.Alpaca](https://github.com/QuantConnect/Lean.Brokerages.Alpaca), [Bybit](https://github.com/QuantConnect/Lean.Brokerages.Bybit) | Source candidates only pending license-file/headers and dependency verification. | Repository metadata did not establish a license in the inspected root response. Do not copy on assumption that all QuantConnect adapters share the core license. |

Infrastructure shortlist, not yet import-approved: [apache/arrow](https://github.com/apache/arrow), [duckdb/duckdb](https://github.com/duckdb/duckdb), [scikit-learn/scikit-learn](https://github.com/scikit-learn/scikit-learn), [scipy/scipy](https://github.com/scipy/scipy), [cvxpy/cvxpy](https://github.com/cvxpy/cvxpy), [pytorch/pytorch](https://github.com/pytorch/pytorch), [microsoft/onnxruntime](https://github.com/microsoft/onnxruntime), [huggingface/safetensors](https://github.com/huggingface/safetensors), [dotnet/aspnetcore](https://github.com/dotnet/aspnetcore), [dotnet/wpf](https://github.com/dotnet/wpf), [facebook/react](https://github.com/facebook/react), [open-telemetry/opentelemetry-dotnet](https://github.com/open-telemetry/opentelemetry-dotnet), [microsoft/playwright](https://github.com/microsoft/playwright). Their exact release/license/transitive compatibility has not been verified in this audit; WP-03 owns the lock/SBOM check before installation. These names express ecosystem fit, not fabricated due diligence. Begin with LEAN indicators, conventional statistics and small estimators; GPU/RL/agent frameworks are optional until measured value justifies their total cost.

Additional official documentation verification: [DuckDB FAQ](https://duckdb.org/faq) confirms MIT licensing, embedded/disk-backed analytical use and supported 1.4 LTS / 1.5 branches at retrieval. Select an exact supported patch and test it; do not require its newer remote protocol for local research. [Apache Arrow overview](https://arrow.apache.org/overview/) confirms cross-language columnar implementations and format integration tests, supporting its use for data interchange. Exact package licenses/dependencies and Windows builds remain WP-03 acceptance. These checks improve the shortlist evidence but do not claim benchmark results or a complete advisory review.

WebView2 is a Microsoft runtime dependency with its own redistribution terms, not a claim that every desktop binary is open source. SQLite has a public-domain core, but selected bindings/extensions carry their own licenses. Data subscriptions, news rights, pretrained model weights and broker SDKs need independent entitlement records. Software license permission does not grant market-data redistribution rights.

## 6. IBKR SDK decision

The official [TWS API changelog](https://www.interactivebrokers.com/docs/tws-api/changelog) states that API 10.49+ uses GPL as of August 3, 2026. An Apache-licensed brokerage wrapper does not erase an underlying SDK's terms. Preferred implementation route is the existing LEAN TWS integration when the exact distribution composition can meet the applicable obligations. If that is incompatible with the chosen distribution policy, use an independently qualified official Web API integration, after proving authentication/session/product coverage. Do not assume that putting GPL code behind IPC automatically resolves the distribution question. Keep this as a narrow adapter packaging gate, not a reason to discard IBKR or change the entire engine.

## 7. Import procedure and cost decision

For every imported component record: source URL, exact revision, fetched source hash, license text and notices, original symbols, changes, destination, transitive dependency lock, security review, characterization tests and owner. A direct dependency is preferred over a source copy when its stable API supplies the needed behavior. Small neutral first-party components may be vendored after rights clearance. Do not make Autosport or Nika a runtime dependency.

Evaluate total cost as adoption + glue + verification + operations + upgrades + licensing/distribution + eventual replacement. No numerical time savings are asserted without an implementation spike. LEAN wins the current qualitative comparison by removing the largest multi-asset engine burden; the adoption gate makes that decision falsifiable.

Migration sequence: characterize A1/B1/B2 → clear provenance → neutralize imports → execute cross-platform fixtures → integrate only into research/artifact paths → remove any accidental financial authority. Engine sequence: pinned build → embedding harness → adapter mock → event reconciliation → financial oracle suite → crash suite → recorded provider fixtures → paper qualification. Candidate failure sends a documented issue to the dependency work package; it does not trigger silent greenfield replacement.

Public advisory coverage is incomplete: several repository advisory endpoints were unavailable during research. No project is labelled vulnerability-free. Dependency/security checks are repeated on the exact release build. Source inspection, official documentation and open issue signals support the architecture; performance, Windows installation and live semantic correctness remain empirical acceptance gates.
