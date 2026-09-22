# AutoTrade — delivery dependencies and implementation bank

Baseline 2026-09-22. This is a plan for future implementation; no work package is represented as implemented. IDs are stable semantic responsibilities, not an activity quota. The matching JSON bank is the machine-readable source for package fields; the expanded entries below are its reading edition.

## 1. Delivery model

The shortest path is a reusable-engine vertical integration plus independent parallel provider, finance, science and UI streams. Build contracts and independent oracles first, then use a simulated provider to exercise the complete authority/execution/recovery path before waiting for every external integration. The complete product scope is retained throughout. Stages describe qualification dependencies, not artificial V1/V2/V3 products.

READY means required input contracts/artifacts exist at identified revisions, no conflicting semantic mutation is owned, and acceptance is defined. A package can perform read-only research/fixture design before source dependencies finish, but cannot claim integrated completion against imaginary dependencies. DONE means merged compatible implementation plus its evidence, not a PR opened or local test passed.

## 2. Stages

| Stage | Objective and dependencies | Contracts; reuse and new work | Parallel work and integration | Tests, risks and definition of done |
|---|---|---|---|---|
| A Foundation and rights | Establish schemas, fixtures, licensing, locks and LEAN adoption; starts from this package | Document 02; pinned LEAN, characterized first-party utilities; new host boundary and provenance manifests | Contracts, dependency review, financial/causal oracle design, semantic UI prototypes can proceed independently | Clean Windows/Linux build, embedding and license evidence. Risk: engine/package mismatch. Done when chosen foundation passes explicit adoption gate and initial contracts are versioned. |
| B Durable financial spine | Depends on A contract/foundation artifacts | Journal, reservations, risk, authority, outbox, order events; LEAN interfaces plus new admission/reconciliation | Persistence, economic projections, risk, data contracts and simulator proceed by stable interfaces | Money conservation, duplicate/UNKNOWN handling, crash boundaries. Risk: two truth authorities. Done when simulator vertical path reconstructs every committed decision and recovers without duplicate exposure. |
| C Data and provider integration | Depends on A/B relevant interfaces, not every unrelated provider | Instruments/capabilities/market/account events; existing qualified brokerage adapters and new missing translations | Six provider teams plus historical/news/data-rights teams; each adapter qualifies separately | Official recorded/sandbox fixtures, rate/clock/reconnect/corrections. Risk: account/API feature mismatch. Done per capability when qualification report names exact build and supported behavior. |
| D Whole financial domain | Depends on economic spine and instrument metadata | Cash, settlement, corporate actions, futures/perpetuals/options, portfolio objective; reuse LEAN/optional valuation oracle | Asset-family accounting/stress/adapter lifecycle teams | Independent vectors, margin/assignment/delivery/FX/cost stress. Risk: unit/multiplier/sign errors. Done when every advertised asset lifecycle conserves money/units and reconciles. |
| E Research, learning and science | Depends on immutable data, replay and episode contracts | Dataset/experience/model/protocol/promotion; LEAN replay and selected scientific libraries; new causal/retention/promotion boundaries | Strategy, feature, memory, routing, continual learning and news attribution | Leakage, walk-forward, multiplicity, retention, cost and zero-LLM tests. Risk: attractive but invalid evidence. Done when a candidate can pass/fail/inconclusive reproducibly and promotion remains within authority. |
| F Accessible operations | Depends on host state/command API; can start against fixtures in A | UiSnapshot/UiCommand, auth/host/recovery; React/WPF/WebView2 and OS secrets; new semantic workflows | Web, desktop, NVDA, security, backup, packaging and telemetry | Keyboard/NVDA, stale state, emergency, install/update/rollback, clean-machine restore. Risk: visual success without usable semantics. Done on delivered signed artifacts, not screenshots. |
| G Integrated qualification | Depends on applicable C–F capability set | Exact release/code/schema/model/data/policy bundle | Provider/asset paper campaigns, science, load/recovery and accessibility audits run in parallel | Forward sealed predictions, reconciliation, cost and incident evidence. Risk: testnet economics mistaken for real fills. Done when each gate has evidence and unresolved failures are explicit. |
| H Authorized real and final acceptance | Depends on G plus active user authority for actual account/build/envelope | Admission and bounded live policy; no new architecture | Independent audits continue; unaffected capabilities need not pause | Actual fills/corrections, no duplicate exposure, recovery and accessible control. Done when whole approved scope works, release evidence is complete and economic claims match evidence. No guaranteed edge is required or invented. |

## 3. Critical path and parallel paths

Structural critical path: contracts/rights → LEAN embedding → journal/economic state → risk/authority → guarded dispatch/order state → reconciliation/simulator → at least one fully qualified end-to-end provider path → complete UI/operations integration → forward paper/recovery → authorized bounded real qualification → whole-product acceptance.

Actual calendar critical path cannot be honestly estimated until the engine-adoption spike and provider access/data availability are measured. Forward evidence length can dominate coding time and should start as soon as a frozen qualified candidate and operational paper path exist. It cannot be compressed by adding more coding workers or repeatedly peeking at outcomes.

Parallel streams: six provider adapters; asset lifecycle models; data/history/news; strategy/features; model routing and memory; accessible web/desktop; financial/scientific oracle teams; security/CI/packaging; recovery/load/audit. They share schema versions and fixture ownership, not a global worker cap. A schema change blocks only its affected consumers.

Integration wave 1 proves deterministic simulator → financial journal → accessible state. Wave 2 proves provider/recovery and full asset accounting. Wave 3 adds causal learning/promotion and shared UI authority. Wave 4 freezes exact qualification candidates/builds for paper, real, recovery and accessibility evidence. Continuous compatible merges continue between waves; waves are coordination points, not global pauses.

## 4. Definition of done and evidence

Every package submits exact source SHA, contract/schema versions, input artifact refs, changes, tests actually run, expected/actual outcomes, meaningful failure cases, dependency/license changes, unresolved limits and integration target. A test inventory is not execution evidence. Review financial/scientific/security/authority changes independently of the implementation author. Failed external provider access is an explicit blocker for that capability, not a reason to fabricate a passing adapter.

Final qualification matrix crosses: provider × account/environment × advertised asset/order capability × replay/paper/live × financial/recovery/UI evidence. Not every provider must expose every asset; every claimed combination must be proven. All requested providers and approved asset families stay in the delivery scope. Inability to access a particular account is reported as unavailable evidence, not converted into a country-based product restriction.

The work-package bank below includes inputs, contracts, modules, dependencies, reuse, tests, acceptance, integration and forbidden/conflicting scope for every package. Later implementation may split a large package only along a meaningful independently testable contract boundary; do not inflate task count.

## 5. Work-package bank

Expanded entries are generated from `08_WORK_PACKAGE_BANK.json` and appended below. They describe proposed files; none of those production files has been created by this architecture task.

### WP-01 — core-schemas

- **Authority family:** CONTRACT
- **Exact scope:** Define all document-02 schemas, OpenAPI and common fixtures with version rules
- **Inputs:** 02_CANONICAL_CONTRACTS.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of the approved engineering baseline
- **Contracts:** Common types; EventEnvelope; all command/event types
- **Likely modules:** contracts/jsonschema/; contracts/openapi/; contracts/fixtures/
- **Dependencies:** None (approved baseline required)
- **Reuse sources:** JSON Schema/OpenAPI tooling
- **Tests:** Cross-language decimal, unknown-field, enum and version fixtures
- **Acceptance:** All three language bindings accept/reject the same corpus; breaking changes require a major version; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through contracts/jsonschema/ and its contract/qualification evidence.
- **Forbidden scope:** Provider-specific financial semantics hidden in generic payloads; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: CONTRACT / core-schemas; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-02 — lean-adoption

- **Authority family:** ENGINE
- **Exact scope:** Embed pinned LEAN C# runtime and prove its integration seam without a second OMS
- **Inputs:** 01_REUSE_AND_OPEN_SOURCE_MASTER_MAP.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-01, WP-03
- **Contracts:** InstrumentVersion; OrderIntent; ExecutionFill
- **Likely modules:** src/AutoTrade.Engine.Lean/; tests/Integration/LeanAdoption
- **Dependencies:** WP-01; WP-03
- **Reuse sources:** QuantConnect/Lean source tree 985ef30ad3ac774218c5ac516b4cb0aa2655730f; resolve associated commit before pinning
- **Tests:** Windows/Linux clean build; callback ordering; decimal; shutdown/restart harness
- **Acceptance:** Reproducible build and simulator integration demonstrate required seams; blockers have measured alternatives; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Engine.Lean/ and its contract/qualification evidence.
- **Forbidden scope:** Greenfield replacement before the adoption evidence; uncontrolled upstream fork; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: ENGINE / lean-adoption; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-03 — dependency-policy

- **Authority family:** PROVENANCE
- **Exact scope:** Resolve exact dependency/model/data license obligations, pins, SBOM and distribution policy
- **Inputs:** 01_REUSE_AND_OPEN_SOURCE_MASTER_MAP.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of the approved engineering baseline
- **Contracts:** EvidenceRef; dependency and rights manifests
- **Likely modules:** provenance/; licenses/; global.json; dependency locks
- **Dependencies:** None (approved baseline required)
- **Reuse sources:** Inspected upstream license texts and metadata
- **Tests:** Notice completeness; transitive resolution; advisory review; clean restore
- **Acceptance:** Each imported byte has a rights basis and hash; unresolved licenses prevent that import; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through provenance/ and its contract/qualification evidence.
- **Forbidden scope:** Assuming public repository implies permission; importing ambiguous adapters; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: PROVENANCE / dependency-policy; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-04 — neutral-first-party

- **Authority family:** REUSE
- **Exact scope:** Characterize and extract approved A1/B1/B2 neutral research utilities
- **Inputs:** 01_REUSE_AND_OPEN_SOURCE_MASTER_MAP.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-01, WP-03
- **Contracts:** Strict import and artifact publication fixtures
- **Likely modules:** research/autotrade_research/io/; research/autotrade_research/artifacts/
- **Dependencies:** WP-01; WP-03
- **Reuse sources:** Autosport json_integrity/integrity/workspace_lock selected symbols
- **Tests:** Malformed JSON; aliases; process death; atomic publish; no Autosport imports
- **Acceptance:** Standalone modules pass old characterization plus new neutral fixtures on Windows/Linux; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through research/autotrade_research/io/ and its contract/qualification evidence.
- **Forbidden scope:** Sports ledger, GUI or orchestration import; financial DB writes through file helpers; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: REUSE / neutral-first-party; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-05 — journal-outbox

- **Authority family:** PERSISTENCE
- **Exact scope:** Implement append-only event journal, command dedupe, versions, outbox and migrations
- **Inputs:** 02_CANONICAL_CONTRACTS.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-01
- **Contracts:** EventEnvelope; JournalTransaction; UiCommand
- **Likely modules:** src/AutoTrade.Persistence/; tests/Recovery/Journal
- **Dependencies:** WP-01
- **Reuse sources:** SQLite transaction/WAL mechanisms
- **Tests:** Crash at commit boundaries; duplicate command changed hash; migration rollback
- **Acceptance:** Committed records survive qualified crashes and projections rebuild identically; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Persistence/ and its contract/qualification evidence.
- **Forbidden scope:** Network send inside DB transaction; mutable source facts; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: PERSISTENCE / journal-outbox; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-06 — immutable-store

- **Authority family:** ARTIFACT
- **Exact scope:** Implement content-addressed artifact manifests, rights-aware export and orphan recovery
- **Inputs:** 02_CANONICAL_CONTRACTS.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-01, WP-04
- **Contracts:** EvidenceRef; DatasetManifest; ModelArtifact
- **Likely modules:** src/AutoTrade.Data/Artifacts/; research/autotrade_research/artifacts/
- **Dependencies:** WP-01; WP-04
- **Reuse sources:** Neutral durable publication helpers; filesystem primitives
- **Tests:** Corrupt hash; orphan temp; crash rename; missing artifact
- **Acceptance:** Only verified committed manifests expose complete artifacts; orphan cleanup cannot delete referenced evidence; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Data/Artifacts/ and its contract/qualification evidence.
- **Forbidden scope:** Artifact hashes presented as admin-proof security; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: ARTIFACT / immutable-store; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-07 — instrument-registry

- **Authority family:** INSTRUMENT
- **Exact scope:** Implement immutable instrument versions, calendars, units and derivative descriptors
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-01, WP-02
- **Contracts:** InstrumentVersion
- **Likely modules:** src/AutoTrade.Data/Instruments/; contracts/fixtures/instruments/
- **Dependencies:** WP-01; WP-02
- **Reuse sources:** LEAN symbols, calendars and security types
- **Tests:** Symbol collision; DST; delisting; adjusted option deliverable; precision bounds
- **Acceptance:** Instrument identity and historical version remain stable across renames and metadata changes; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Data/Instruments/ and its contract/qualification evidence.
- **Forbidden scope:** Ticker as global identity; default 100-share option assumption; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: INSTRUMENT / instrument-registry; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-08 — account-capabilities

- **Authority family:** CAPABILITY
- **Exact scope:** Intersect documented/API/account/instrument evidence with expiry and conflict handling
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-01, WP-07
- **Contracts:** CapabilitySnapshot
- **Likely modules:** src/AutoTrade.Providers.Abstractions/Capabilities/
- **Dependencies:** WP-01; WP-07
- **Reuse sources:** Existing brokerage metadata where qualified
- **Tests:** Unknown/expired permissions; mode change; conflicting instrument filters
- **Acceptance:** Only current evidenced capability can admit its matching action; all providers remain represented; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Providers.Abstractions/Capabilities/ and its contract/qualification evidence.
- **Forbidden scope:** Country-based provider exclusion; unsupported action coercion; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: CAPABILITY / account-capabilities; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-09 — market-normalization

- **Authority family:** DATA
- **Exact scope:** Normalize quotes/trades/books/bars/funding with sequence/freshness quality
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-06, WP-07
- **Contracts:** MarketEvent; InstrumentVersion
- **Likely modules:** src/AutoTrade.Data/Market/; tests/Contracts/Market
- **Dependencies:** WP-06; WP-07
- **Reuse sources:** LEAN data interfaces; existing qualified parsers
- **Tests:** Snapshot/delta gap; checksum; crossed book; late/revised bar
- **Acceptance:** Unusable market state blocks affected new risk and recovers through a verified snapshot; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Data/Market/ and its contract/qualification evidence.
- **Forbidden scope:** Inventing ticks from OHLC; using future finalized bars; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: DATA / market-normalization; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-10 — historical-vintages

- **Authority family:** DATA
- **Exact scope:** Build lawful immutable historical datasets and point-in-time universe/revisions
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-09
- **Contracts:** DatasetManifest; MarketEvent
- **Likely modules:** src/AutoTrade.Data/History/; research/autotrade_research/data/
- **Dependencies:** WP-09
- **Reuse sources:** Parquet/Arrow/DuckDB after WP-03; provider history APIs
- **Tests:** Missingness; delisted assets; adjusted/raw consistency; vintage cutoff
- **Acceptance:** Manifest reconstructs exact historical view and explicitly states availability limitations; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Data/History/ and its contract/qualification evidence.
- **Forbidden scope:** Replacing old vintages with latest revised data; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: DATA / historical-vintages; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-11 — news-macro-claims

- **Authority family:** INFORMATION
- **Exact scope:** Ingest and extract time/provenance/rights-bound news, macro and corporate claims
- **Inputs:** 05_LEARNING_MEMORY_AI_AND_AGENT_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-06, WP-09
- **Contracts:** InformationEvent; EvidenceRef
- **Likely modules:** src/AutoTrade.Information/
- **Dependencies:** WP-06; WP-09
- **Reuse sources:** Official feeds and permitted source parsers
- **Tests:** Syndication duplicates; conflicting claims; late publication; prompt content
- **Acceptance:** Claims retain passage evidence/time and cannot grant permissions; revisions preserve history; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Information/ and its contract/qualification evidence.
- **Forbidden scope:** Unlicensed redistribution; source popularity as evidence of edge; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: INFORMATION / news-macro-claims; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-12 — causal-feeder

- **Authority family:** REPLAY
- **Exact scope:** Implement clock, causal data view, isolated feeder, deterministic checkpoint/resume
- **Inputs:** 06_REPLAY_SCIENTIFIC_VALIDATION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-02, WP-09, WP-10
- **Contracts:** MarketEvent; DatasetManifest; ExperienceEpisode
- **Likely modules:** research/autotrade_research/evaluation/replay/; src/AutoTrade.Engine.Lean/Replay/
- **Dependencies:** WP-02; WP-09; WP-10
- **Reuse sources:** LEAN replay interfaces; Autosport firewall concepts/tests
- **Tests:** Future-file access; same-time ordering; resume equivalence; bar close timing
- **Acceptance:** Strategy receives only evidenced available data; resumed decisions/ledger match baseline; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through research/autotrade_research/evaluation/replay/ and its contract/qualification evidence.
- **Forbidden scope:** In-process conventions claimed as hostile-code isolation; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: REPLAY / causal-feeder; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-13 — execution-realism

- **Authority family:** SIMULATION
- **Exact scope:** Qualify fill/fee/slippage/latency/impact assumptions by asset and data fidelity
- **Inputs:** 06_REPLAY_SCIENTIFIC_VALIDATION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-02, WP-12
- **Contracts:** ExecutionFill; InstrumentVersion; ExperimentProtocol
- **Likely modules:** src/AutoTrade.Engine.Lean/Simulation/; tests/Finance/Simulation
- **Dependencies:** WP-02; WP-12
- **Reuse sources:** LEAN fill/fee/slippage models
- **Tests:** Impossible same-bar fills; partials; latency; adverse cost scenario
- **Acceptance:** Simulator reports fidelity bounds and passes conservative independent fill oracles; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Engine.Lean/Simulation/ and its contract/qualification evidence.
- **Forbidden scope:** Paper/testnet fills labelled proof of live profitability; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: SIMULATION / execution-realism; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-14 — economic-ledger

- **Authority family:** FINANCE
- **Exact scope:** Implement cash/inventory/P&L/FX postings, corrections and state projections
- **Inputs:** 04_PORTFOLIO_RISK_ECONOMIC_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-02, WP-05, WP-07
- **Contracts:** JournalTransaction; Position/CashSnapshot
- **Likely modules:** src/AutoTrade.Portfolio/Accounting/; tests/Finance/Accounting
- **Dependencies:** WP-02; WP-05; WP-07
- **Reuse sources:** LEAN portfolio mechanisms plus independent document-04 vectors
- **Tests:** Round trip; third-currency fee; rebate; deposit; bust/correction
- **Acceptance:** All unit/currency conservation and exact reference vectors pass; provider/local values stay distinguishable; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Portfolio/Accounting/ and its contract/qualification evidence.
- **Forbidden scope:** Sports win/loss ledger; silent float money coercion; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: FINANCE / economic-ledger; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-15 — reservations

- **Authority family:** FINANCE
- **Exact scope:** Implement current/working/UNKNOWN exposure reservation and release
- **Inputs:** 04_PORTFOLIO_RISK_ECONOMIC_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-14
- **Contracts:** RiskDecision; AdmissionRecord; OrderIntent
- **Likely modules:** src/AutoTrade.Portfolio/Reservations/
- **Dependencies:** WP-14
- **Reuse sources:** SQLite atomic state versioning
- **Tests:** Two concurrent intents; partial fill/cancel; overlapping replacement
- **Acceptance:** No double spending or premature release; reservations reconcile into positions and terminal remainder; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Portfolio/Reservations/ and its contract/qualification evidence.
- **Forbidden scope:** Ignoring pending cancel/unknown/manual exposure; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: FINANCE / reservations; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-16 — independent-risk

- **Authority family:** RISK
- **Exact scope:** Implement hard policy, freshness, concentration, stress and margin checks
- **Inputs:** 04_PORTFOLIO_RISK_ECONOMIC_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-08, WP-15
- **Contracts:** RiskDecision; PortfolioTarget; CapabilitySnapshot
- **Likely modules:** src/AutoTrade.Risk/
- **Dependencies:** WP-08; WP-15
- **Reuse sources:** LEAN buying power interfaces; independent stress fixtures
- **Tests:** Stale FX; correlated shocks; leverage; borrow; option exercise; rule-boundary properties
- **Acceptance:** Each verdict is reproducible from current state and cannot be overridden by strategy/model output; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Risk/ and its contract/qualification evidence.
- **Forbidden scope:** Risk penalties posted as cash expenses; VaR-only safety claims; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: RISK / independent-risk; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-17 — policy-confirmation

- **Authority family:** AUTHORITY
- **Exact scope:** Implement confirmation binding, autonomous policy, expiry/revocation and admissions
- **Inputs:** 02_CANONICAL_CONTRACTS.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-05, WP-16
- **Contracts:** AuthorityPolicy; Confirmation; AdmissionRecord
- **Likely modules:** src/AutoTrade.Execution/Authority/
- **Dependencies:** WP-05; WP-16
- **Reuse sources:** Autosport authority concepts only
- **Tests:** Changed intent hash; expiry; concurrent revoke; protection policy
- **Acceptance:** Exact authority scope enforced at durable admission and dispatcher barrier; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Execution/Authority/ and its contract/qualification evidence.
- **Forbidden scope:** Learning or agent votes modifying user authority; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: AUTHORITY / policy-confirmation; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-18 — guarded-dispatch

- **Authority family:** EXECUTION
- **Exact scope:** Implement last-send guard, durable attempts and provider-compatible client IDs
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-02, WP-17
- **Contracts:** OrderIntent; AdmissionRecord; SubmissionAttempt
- **Likely modules:** src/AutoTrade.Execution/Dispatch/; src/AutoTrade.Engine.Lean/GuardedBrokerage/
- **Dependencies:** WP-02; WP-17
- **Reuse sources:** LEAN IBrokerage seam; execution-ledger UNKNOWN concepts
- **Tests:** Crash before/after send; revoke race; duplicate outbox; timeout
- **Acceptance:** No unsent revoked action crosses barrier; ambiguous sends stay reserved and reconcile; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Execution/Dispatch/ and its contract/qualification evidence.
- **Forbidden scope:** Blind retry; claiming exactly-once external execution; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: EXECUTION / guarded-dispatch; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-19 — order-projection

- **Authority family:** EXECUTION
- **Exact scope:** Implement order/action lifecycle, fills, cancel/amend lineage and OCO race handling
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-18
- **Contracts:** ExecutionFill; SubmissionResult; OrderIntent
- **Likely modules:** src/AutoTrade.Execution/Orders/
- **Dependencies:** WP-18
- **Reuse sources:** LEAN order events; provider-neutral fixtures
- **Tests:** Fill-before-ack; partial cancel; duplicate Filled; late correction; two OCO fills
- **Acceptance:** Unique fills alone post economics; request acknowledgement never becomes invented fill; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Execution/Orders/ and its contract/qualification evidence.
- **Forbidden scope:** Terminal status suppressing later economic corrections; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: EXECUTION / order-projection; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-20 — account-truth

- **Authority family:** RECONCILIATION
- **Exact scope:** Reconcile provider snapshots/activity, unknown sends, manual orders and gaps
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-14, WP-19
- **Contracts:** ReconciliationRun; account snapshots; ExecutionFill
- **Likely modules:** src/AutoTrade.Execution/Reconciliation/
- **Dependencies:** WP-14; WP-19
- **Reuse sources:** Provider read APIs; first-party absence-proof concepts
- **Tests:** Incomplete pagination; eventual history; manual fill; late fee; inconsistent snapshot
- **Acceptance:** PROVEN_ABSENT requires coverage evidence; unresolved differences block affected new risk; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Execution/Reconciliation/ and its contract/qualification evidence.
- **Forbidden scope:** Empty recent-order page treated as definitive absence; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: RECONCILIATION / account-truth; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-21 — simulated-provider

- **Authority family:** PROVIDER
- **Exact scope:** Provide deterministic official-contract-shaped simulated provider for integration
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-08, WP-19
- **Contracts:** All Provider interface methods
- **Likely modules:** tests/Providers/Simulator/; tests/Providers/ContractHarness/
- **Dependencies:** WP-08; WP-19
- **Reuse sources:** LEAN simulation; canonical fixtures
- **Tests:** Rejection, quota, clock, outage, ambiguity and correction scenario matrix
- **Acceptance:** Every interface can exercise success/failure/UNKNOWN deterministically without live credentials; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through tests/Providers/Simulator/ and its contract/qualification evidence.
- **Forbidden scope:** Simulated success used as real-provider qualification; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: PROVIDER / simulated-provider; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-22 — bybit-adapter

- **Authority family:** PROVIDER
- **Exact scope:** Qualify Bybit V5 public/private data, account, orders and supported product categories
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-08, WP-18, WP-20, WP-21
- **Contracts:** Provider interface; Bybit capability fixtures
- **Likely modules:** src/AutoTrade.Providers.Bybit/; tests/Providers/Bybit/
- **Dependencies:** WP-08; WP-18; WP-20; WP-21
- **Reuse sources:** LEAN Bybit candidate only after license check; official V5
- **Tests:** Async ack; duplicate Filled; history lag; category and position-mode tests
- **Acceptance:** Exact feature matrix and test/demo evidence; unsupported features explicit; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Providers.Bybit/ and its contract/qualification evidence.
- **Forbidden scope:** Invented sandbox parity; copying unclear-license wrapper; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: PROVIDER / bybit-adapter; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-23 — kraken-adapter

- **Authority family:** PROVIDER
- **Exact scope:** Qualify separate Spot and Derivatives adapters under one provider family
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-08, WP-18, WP-20, WP-21
- **Contracts:** Provider interface; Kraken capability fixtures
- **Likely modules:** src/AutoTrade.Providers.Kraken/; tests/Providers/Kraken/
- **Dependencies:** WP-08; WP-18; WP-20; WP-21
- **Reuse sources:** LEAN Kraken; official Spot/Derivatives APIs
- **Tests:** Nonce collision; quota tiers; derivatives demo; reconnect/order queries
- **Acceptance:** Spot/derivative identities/auth/limits remain distinct and reconcile; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Providers.Kraken/ and its contract/qualification evidence.
- **Forbidden scope:** Assumed public spot sandbox; withdrawals in agent tools; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: PROVIDER / kraken-adapter; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-24 — whitebit-adapter

- **Authority family:** PROVIDER
- **Exact scope:** Qualify WhiteBIT spot/collateral streams, REST and exact order semantics
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-08, WP-18, WP-20, WP-21
- **Contracts:** Provider interface; WhiteBIT capability fixtures
- **Likely modules:** src/AutoTrade.Providers.WhiteBIT/; tests/Providers/WhiteBIT/
- **Dependencies:** WP-08; WP-18; WP-20; WP-21
- **Reuse sources:** Official API; CCXT transport only if proven equivalent
- **Tests:** Partial slippage-band cancel; reduce-only resizing; endpoint-specific conditions
- **Acceptance:** Account-discovered capability matrix and full execution/reconciliation evidence; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Providers.WhiteBIT/ and its contract/qualification evidence.
- **Forbidden scope:** Country-based product exclusion; unverified universal test environment; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: PROVIDER / whitebit-adapter; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-25 — binance-adapter

- **Authority family:** PROVIDER
- **Exact scope:** Qualify separate Binance product families, filters and stream/order semantics
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-08, WP-18, WP-20, WP-21
- **Contracts:** Provider interface; Binance capability fixtures
- **Likely modules:** src/AutoTrade.Providers.Binance/; tests/Providers/Binance/
- **Dependencies:** WP-08; WP-18; WP-20; WP-21
- **Reuse sources:** LEAN Binance; official API specifications
- **Tests:** Filter changes; weighted quotas; spot versus futures mode; reconnect
- **Acceptance:** Each advertised product/environment has independent recorded/sandbox qualification; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Providers.Binance/ and its contract/qualification evidence.
- **Forbidden scope:** One spot adapter silently advertised as futures/options support; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: PROVIDER / binance-adapter; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-26 — ibkr-adapter

- **Authority family:** PROVIDER
- **Exact scope:** Qualify TWS route with distribution gate or approved official Web API alternative
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-03, WP-08, WP-18, WP-20, WP-21
- **Contracts:** Provider interface; IBKR contract/activity fixtures
- **Likely modules:** src/AutoTrade.Providers.IBKR/; tests/Providers/IBKR/
- **Dependencies:** WP-03; WP-08; WP-18; WP-20; WP-21
- **Reuse sources:** LEAN IBKR subject to exact SDK terms
- **Tests:** Session restart; contract IDs; manual orders; executions; lifecycle/pacing
- **Acceptance:** Working entitled account path and compliant exact SDK composition; no unproved API equivalence; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Providers.IBKR/ and its contract/qualification evidence.
- **Forbidden scope:** IPC asserted to erase GPL obligations; ticker-only routing; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: PROVIDER / ibkr-adapter; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-27 — alpaca-adapter

- **Authority family:** PROVIDER
- **Exact scope:** Qualify Alpaca account/orders/activities and entitled equity/crypto/options
- **Inputs:** 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-08, WP-18, WP-20, WP-21
- **Contracts:** Provider interface; Alpaca capability fixtures
- **Likely modules:** src/AutoTrade.Providers.Alpaca/; tests/Providers/Alpaca/
- **Dependencies:** WP-08; WP-18; WP-20; WP-21
- **Reuse sources:** Official APIs; LEAN candidate after rights verification
- **Tests:** Client ID; bracket race; option levels; polled assignment; delayed paper NTA
- **Acceptance:** Activities and streams jointly reconcile; paper realism limitations exposed; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Providers.Alpaca/ and its contract/qualification evidence.
- **Forbidden scope:** Assuming assignment always arrives on order WebSocket; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: PROVIDER / alpaca-adapter; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-28 — futures-lifecycle

- **Authority family:** FINANCE
- **Exact scope:** Implement linear/inverse futures multiplier, variation margin, expiry and delivery policy
- **Inputs:** 04_PORTFOLIO_RISK_ECONOMIC_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-07, WP-13, WP-14
- **Contracts:** InstrumentVersion; JournalTransaction; MarginSnapshot
- **Likely modules:** src/AutoTrade.Portfolio/Futures/; tests/Finance/Futures/
- **Dependencies:** WP-07; WP-13; WP-14
- **Reuse sources:** LEAN futures and document-04 independent formulas
- **Tests:** Tick value; roll; inverse payoff; settlement; first-notice cutoff
- **Acceptance:** No double-counted variation P&L; delivery exposure explicitly prevented or qualified; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Portfolio/Futures/ and its contract/qualification evidence.
- **Forbidden scope:** Continuous backadjusted series used as executable contract; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: FINANCE / futures-lifecycle; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-29 — perpetual-lifecycle

- **Authority family:** FINANCE
- **Exact scope:** Implement funding, mark/index, collateral conversion and liquidation stress
- **Inputs:** 04_PORTFOLIO_RISK_ECONOMIC_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-28
- **Contracts:** InstrumentVersion; funding MarketEvent; MarginSnapshot
- **Likely modules:** src/AutoTrade.Portfolio/Perpetuals/; tests/Finance/Perpetuals/
- **Dependencies:** WP-28
- **Reuse sources:** LEAN hooks; official provider funding/margin metadata
- **Tests:** Funding signs/currency; depeg; tier changes; unavailable exit
- **Acceptance:** Funding reconciles once, payoff units are correct and stale margin blocks new risk; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Portfolio/Perpetuals/ and its contract/qualification evidence.
- **Forbidden scope:** One universal funding convention assumed across venues; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: FINANCE / perpetual-lifecycle; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-30 — options-lifecycle

- **Authority family:** FINANCE
- **Exact scope:** Implement Greeks/scenarios, adjusted deliverables, exercise/assignment and multi-leg risk
- **Inputs:** 04_PORTFOLIO_RISK_ECONOMIC_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-07, WP-13, WP-14
- **Contracts:** InstrumentVersion; JournalTransaction; RiskDecision
- **Likely modules:** src/AutoTrade.Portfolio/Options/; tests/Finance/Options/
- **Dependencies:** WP-07; WP-13; WP-14
- **Reuse sources:** LEAN options; optional QuantLib/QLNet verified oracle
- **Tests:** Call/put expiry; early assignment; adjusted contract; partial combo
- **Acceptance:** Exercise/assignment conservation and interim-leg margin/protection pass; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Portfolio/Options/ and its contract/qualification evidence.
- **Forbidden scope:** Premium equated with maximum short-option loss; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: FINANCE / options-lifecycle; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-31 — corporate-settlement

- **Authority family:** FINANCE
- **Exact scope:** Implement equity corporate actions, settled cash, borrow/recall and financing
- **Inputs:** 04_PORTFOLIO_RISK_ECONOMIC_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-10, WP-14
- **Contracts:** InstrumentVersion; JournalTransaction; CashSnapshot
- **Likely modules:** src/AutoTrade.Portfolio/CorporateActions/; tests/Finance/CorporateActions/
- **Dependencies:** WP-10; WP-14
- **Reuse sources:** LEAN corporate actions/calendars
- **Tests:** Split/dividend/merger/delist; unsettled buy; recall and fee
- **Acceptance:** Holdings/basis/cash reconcile with no double adjustment or deposit-return inflation; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Portfolio/CorporateActions/ and its contract/qualification evidence.
- **Forbidden scope:** Tax residence inferred; backadjusted data mutating live holdings twice; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: FINANCE / corporate-settlement; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-32 — allocation-objective

- **Authority family:** PORTFOLIO
- **Exact scope:** Implement feasible cost/risk-aware portfolio targets and instrument selection
- **Inputs:** 04_PORTFOLIO_RISK_ECONOMIC_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-14, WP-16
- **Contracts:** PortfolioTarget; DecisionProposal; RiskDecision
- **Likely modules:** src/AutoTrade.Portfolio/Allocation/
- **Dependencies:** WP-14; WP-16
- **Reuse sources:** LEAN portfolio interfaces; optional qualified numerical solver
- **Tests:** Infeasible constraints; correlation stress; minimum lots; solver timeout
- **Acceptance:** Feasible verified allocation or explicit no-increase fallback; cash is valid; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Portfolio/Allocation/ and its contract/qualification evidence.
- **Forbidden scope:** Spending unfunded account capital; optimizing win rate only; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: PORTFOLIO / allocation-objective; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-33 — deterministic-path

- **Authority family:** STRATEGY
- **Exact scope:** Implement useful zero-LLM strategy descriptors and transparent baseline families
- **Inputs:** 05_LEARNING_MEMORY_AI_AND_AGENT_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-12, WP-14
- **Contracts:** DecisionProposal; strategy descriptor; causal view
- **Likely modules:** research/autotrade_research/strategies/
- **Dependencies:** WP-12; WP-14
- **Reuse sources:** LEAN indicators and conventional estimators
- **Tests:** No model endpoints; horizon/cutoff; strategy state resume
- **Acceptance:** Research→paper proposal path works with zero model calls and no fabricated edge; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through research/autotrade_research/strategies/ and its contract/qualification evidence.
- **Forbidden scope:** Hard-coded daily return/trade-count targets; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: STRATEGY / deterministic-path; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-34 — causal-features

- **Authority family:** FEATURE
- **Exact scope:** Implement fit-per-fold features, cross-market/regime inputs and label timing
- **Inputs:** 06_REPLAY_SCIENTIFIC_VALIDATION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-10, WP-11, WP-12
- **Contracts:** DatasetManifest; feature schema; DecisionProposal
- **Likely modules:** research/autotrade_research/features/
- **Dependencies:** WP-10; WP-11; WP-12
- **Reuse sources:** LEAN indicators; qualified scientific libraries
- **Tests:** Normalizer leakage; delayed labels; missing assets; source revisions
- **Acceptance:** Every feature/label has reproducible cutoff and training lineage; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through research/autotrade_research/features/ and its contract/qualification evidence.
- **Forbidden scope:** Global fit on train+test; future regime classifier labels; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: FEATURE / causal-features; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-35 — protocol-registry

- **Authority family:** SCIENCE
- **Exact scope:** Implement registered experiments, complete trials and holdout-access accounting
- **Inputs:** 06_REPLAY_SCIENTIFIC_VALIDATION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-05, WP-06, WP-12
- **Contracts:** ExperimentProtocol; EvaluationResult
- **Likely modules:** src/AutoTrade.Science/Registry/
- **Dependencies:** WP-05; WP-06; WP-12
- **Reuse sources:** Autosport scientific fingerprints/concepts; optional Optuna
- **Tests:** Discarded failures; repeat holdout; changed post-result thresholds
- **Acceptance:** No unlogged trial/access can appear as an untouched locked evaluation; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Science/Registry/ and its contract/qualification evidence.
- **Forbidden scope:** Mutable experiment folder as canonical truth; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: SCIENCE / protocol-registry; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-36 — evaluation-gates

- **Authority family:** SCIENCE
- **Exact scope:** Implement document-06 gates, uncertainty, selection controls and baselines
- **Inputs:** 06_REPLAY_SCIENTIFIC_VALIDATION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-13, WP-33, WP-34, WP-35
- **Contracts:** GateProfile; EvaluationResult; ExperimentProtocol
- **Likely modules:** research/autotrade_research/evaluation/; src/AutoTrade.Science/Gates/
- **Dependencies:** WP-13; WP-33; WP-34; WP-35
- **Reuse sources:** Statistical libraries; DSR as diagnostic only
- **Tests:** Dependent returns; cost stress; missing trials; insufficient power
- **Acceptance:** PASS/FAIL/INCONCLUSIVE follows pre-registered rules with complete evidence; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through research/autotrade_research/evaluation/ and its contract/qualification evidence.
- **Forbidden scope:** Green software tests treated as proof of profitability; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: SCIENCE / evaluation-gates; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-37 — cumulative-episodes

- **Authority family:** MEMORY
- **Exact scope:** Implement immutable experience/knowledge/failure memory with derived indexes
- **Inputs:** 05_LEARNING_MEMORY_AI_AND_AGENT_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-06, WP-11, WP-20, WP-35
- **Contracts:** ExperienceEpisode; InformationEvent; EvidenceRef
- **Likely modules:** src/AutoTrade.Learning/Memory/
- **Dependencies:** WP-06; WP-11; WP-20; WP-35
- **Reuse sources:** SQLite full-text; Nika memory concepts only
- **Tests:** Correction/tombstone; permission/cutoff filter; duplicate episode
- **Acceptance:** Old evidence preserved or explicitly tombstoned; retrieval cites valid source versions; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Learning/Memory/ and its contract/qualification evidence.
- **Forbidden scope:** Destructive upsert substituted for cumulative memory; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: MEMORY / cumulative-episodes; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-38 — continual-candidates

- **Authority family:** LEARNING
- **Exact scope:** Implement bounded online updates, periodic/offline candidates and retention matrix
- **Inputs:** 05_LEARNING_MEMORY_AI_AND_AGENT_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-36, WP-37
- **Contracts:** ModelArtifact; ExperienceEpisode; EvaluationResult
- **Likely modules:** research/autotrade_research/learning/; src/AutoTrade.Learning/Candidates/
- **Dependencies:** WP-36; WP-37
- **Reuse sources:** River; rehearsal/regularization techniques when measured useful
- **Tests:** Recent gain/old-regime loss; drift false alarm; delayed labels
- **Acceptance:** Candidate cannot promote outside gates; retention regression visible and enforced; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through research/autotrade_research/learning/ and its contract/qualification evidence.
- **Forbidden scope:** Archive existence claimed as proof of no forgetting; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: LEARNING / continual-candidates; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-39 — routing-budgets

- **Authority family:** MODEL
- **Exact scope:** Implement zero/local/fixed/allowlist/dynamic model routing with privacy and costs
- **Inputs:** 05_LEARNING_MEMORY_AI_AND_AGENT_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-05, WP-06
- **Contracts:** ModelRequest; ModelResponse; AuthorityPolicy
- **Likely modules:** src/AutoTrade.ModelGateway/
- **Dependencies:** WP-05; WP-06
- **Reuse sources:** Nika provider/secret test concepts; approved inference APIs
- **Tests:** Local-only remote denial; invalid output; cancellation; exhausted budget
- **Acceptance:** Actual model identity/cost/deadline recorded; deterministic fallback honors policy; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.ModelGateway/ and its contract/qualification evidence.
- **Forbidden scope:** Silent remote fallback or unapproved weight download; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: MODEL / routing-budgets; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-40 — specialist-dag

- **Authority family:** AGENT
- **Exact scope:** Implement typed specialist roles, critique and measured aggregation
- **Inputs:** 05_LEARNING_MEMORY_AI_AND_AGENT_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-33, WP-34, WP-39
- **Contracts:** DecisionProposal; ModelRequest; PortfolioTarget
- **Likely modules:** src/AutoTrade.Learning/Agents/
- **Dependencies:** WP-33; WP-34; WP-39
- **Reuse sources:** Deterministic functions and qualified model gateway
- **Tests:** Conflicting evidence; correlated votes; deadline; incremental value ablation
- **Acceptance:** Only useful roles run within budget; no role can bypass risk/authority; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Learning/Agents/ and its contract/qualification evidence.
- **Forbidden scope:** Fixed decorative agent count; majority vote grants trading permission; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: AGENT / specialist-dag; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-41 — durable-research-jobs

- **Authority family:** JOB
- **Exact scope:** Implement research job leases, checkpoints, idempotent publication and resource budgets
- **Inputs:** 05_LEARNING_MEMORY_AI_AND_AGENT_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-05, WP-06
- **Contracts:** Job record; EvidenceRef; ModelArtifact
- **Likely modules:** src/AutoTrade.Jobs/
- **Dependencies:** WP-05; WP-06
- **Reuse sources:** OS processes; optional qualified scheduler
- **Tests:** Lease expiry; crash mid-artifact; duplicate worker; cancellation
- **Acceptance:** At most one accepted result per job version; resumable work and bounded resources; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Jobs/ and its contract/qualification evidence.
- **Forbidden scope:** Financial order submission as a generic automatically retried job; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: JOB / durable-research-jobs; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-42 — champion-control

- **Authority family:** PROMOTION
- **Exact scope:** Implement independent gated promotion, scoped online envelopes and rollback
- **Inputs:** 06_REPLAY_SCIENTIFIC_VALIDATION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-17, WP-36, WP-38, WP-41
- **Contracts:** PromotionDecision; AuthorityPolicy; EvaluationResult
- **Likely modules:** src/AutoTrade.Science/Promotion/
- **Dependencies:** WP-17; WP-36; WP-38; WP-41
- **Reuse sources:** Immutable model manifests; compare-and-swap pointer
- **Tests:** Concurrent promotion; expired evidence; rollback with open position
- **Acceptance:** Future routing switches atomically while existing position management remains explicit; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Science/Promotion/ and its contract/qualification evidence.
- **Forbidden scope:** Promotion expands user authority/hard risk or deploys arbitrary code; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: PROMOTION / champion-control; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-43 — host-state-commands

- **Authority family:** API
- **Exact scope:** Implement authenticated versioned host API, command outcomes and resumable events
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-05, WP-17, WP-19, WP-20
- **Contracts:** UiSnapshot; UiCommand; all read projections
- **Likely modules:** src/AutoTrade.Host/Api/
- **Dependencies:** WP-05; WP-17; WP-19; WP-20
- **Reuse sources:** ASP.NET Core after dependency review
- **Tests:** Idempotency conflict; stale state; event gap; multiple UI sessions
- **Acceptance:** Both interfaces observe same durable state and distinguish accepted from completed; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Host/Api/ and its contract/qualification evidence.
- **Forbidden scope:** UI client as financial source of truth; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: API / host-state-commands; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-44 — semantic-web

- **Authority family:** UI
- **Exact scope:** Implement accessible web workflows, tables, explanations and notifications
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-43
- **Contracts:** UiSnapshot; UiCommand
- **Likely modules:** web/src/; web/tests/
- **Dependencies:** WP-43
- **Reuse sources:** React semantic HTML; approved accessible primitives
- **Tests:** Keyboard/focus; zoom; label/errors; live-region flood; stale values
- **Acceptance:** All critical workflows and copyable values available without charts/mouse; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through web/src/ and its contract/qualification evidence.
- **Forbidden scope:** Custom inaccessible grid/visual-only risk status; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: UI / semantic-web; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-45 — windows-shell

- **Authority family:** DESKTOP
- **Exact scope:** Implement WPF/WebView2 shell, host lifecycle and native emergency/status surface
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-44
- **Contracts:** UiSnapshot; UiCommand; host pairing
- **Likely modules:** src/AutoTrade.Desktop/
- **Dependencies:** WP-44
- **Reuse sources:** WPF/WebView2 with verified redistribution
- **Tests:** Webview crash; host restart; focus; native stop; disconnected command
- **Acceptance:** Desktop shares state and remains truthful/usable through UI failure; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Desktop/ and its contract/qualification evidence.
- **Forbidden scope:** Separate desktop trading engine or local-PC-off uptime claim; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: DESKTOP / windows-shell; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-46 — secrets-auth

- **Authority family:** SECURITY
- **Exact scope:** Implement scoped secret storage, roles, sessions, local pairing and remote TLS
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-03, WP-05
- **Contracts:** AuthorityPolicy; credential handles; UiCommand
- **Likely modules:** src/AutoTrade.Host/Security/; src/AutoTrade.Execution/Credentials/
- **Dependencies:** WP-03; WP-05
- **Reuse sources:** Windows identity protection; qualified host secret store
- **Tests:** Redaction; wrong identity; token expiry; origin/role checks; rotation
- **Acceptance:** Research/model/UI payloads never receive trade secrets; withdrawal tools absent; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Host/Security/ and its contract/qualification evidence.
- **Forbidden scope:** Loopback assumed unauthenticated-safe; keys in logs/artifacts; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: SECURITY / secrets-auth; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-47 — decision-traces

- **Authority family:** OBSERVABILITY
- **Exact scope:** Implement correlated financial traces, metrics and accessible diagnostic exports
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-05
- **Contracts:** EventEnvelope; EvidenceRef; UiSnapshot
- **Likely modules:** src/AutoTrade.Host/Observability/
- **Dependencies:** WP-05
- **Reuse sources:** OpenTelemetry-compatible libraries after review
- **Tests:** Missing trace linkage; redaction; replay reconstruction; metric backlog
- **Acceptance:** A decision is reconstructed from evidence without depending on sampled logs; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Host/Observability/ and its contract/qualification evidence.
- **Forbidden scope:** Logs substituted for durable financial journal; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: OBSERVABILITY / decision-traces; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-48 — runtime-failure-control

- **Authority family:** RECOVERY
- **Exact scope:** Implement restart, degraded states, disk/clock handling and host ownership transfer
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-20, WP-46
- **Contracts:** ReconciliationRun; owner fence; AdmissionRecord
- **Likely modules:** src/AutoTrade.Execution/Recovery/; tests/Recovery/Host
- **Dependencies:** WP-20; WP-46
- **Reuse sources:** SQLite recovery; local lock concepts; provider reconciliation
- **Tests:** Crash all send points; stale lease; full disk; clock jump; old sender
- **Acceptance:** No duplicate exposure or false READY; external uncertainty remains visible; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Execution/Recovery/ and its contract/qualification evidence.
- **Forbidden scope:** Automatic failover based only on lease timeout; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: RECOVERY / runtime-failure-control; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-49 — backup-restore

- **Authority family:** RECOVERY
- **Exact scope:** Implement consistent DB/artifact backup, verification and clean-machine restore
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-05, WP-06
- **Contracts:** Artifact manifests; schema versions; ReconciliationRun
- **Likely modules:** src/AutoTrade.Persistence/Backup/; tests/Recovery/Restore
- **Dependencies:** WP-05; WP-06
- **Reuse sources:** SQLite backup API; cryptographic hashes
- **Tests:** WAL-active backup; missing blob; incompatible schema; interrupted restore
- **Acceptance:** Restored state verifies and cannot trade before reconciliation/fencing; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through src/AutoTrade.Persistence/Backup/ and its contract/qualification evidence.
- **Forbidden scope:** Copying only live DB main file; promising zero loss after unbacked disk destruction; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: RECOVERY / backup-restore; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-50 — windows-packaging

- **Authority family:** RELEASE
- **Exact scope:** Implement reproducible signed installer, prerequisites, update and rollback
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-02, WP-45, WP-46, WP-49
- **Contracts:** Release manifest; schema compatibility; host state
- **Likely modules:** packaging/windows/; build/; deploy/
- **Dependencies:** WP-02; WP-45; WP-46; WP-49
- **Reuse sources:** .NET packaging and WebView2 supported distribution
- **Tests:** Clean Windows install; failed migration; downgrade compatibility; uninstall/data choice
- **Acceptance:** Keyboard-operable signed artifacts update/restore safely on clean target; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through packaging/windows/ and its contract/qualification evidence.
- **Forbidden scope:** Release claimed from source-only build; old binary on incompatible DB; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: RELEASE / windows-packaging; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-51 — quality-pipeline

- **Authority family:** CI
- **Exact scope:** Implement contract, unit/property, finance/science, provider and Windows CI selection
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-01, WP-03
- **Contracts:** All fixtures; evidence manifest
- **Likely modules:** .github/workflows/; tests/Integration/
- **Dependencies:** WP-01; WP-03
- **Reuse sources:** Existing ecosystem test runners; reusable fixtures
- **Tests:** Intentional invariant violations; generated drift; secret isolation
- **Acceptance:** Mandatory gates detect known-invalid builds and tie reports to exact head; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through .github/workflows/ and its contract/qualification evidence.
- **Forbidden scope:** Live/signing secrets in untrusted PR tests; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: CI / quality-pipeline; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-52 — untrusted-input-boundaries

- **Authority family:** SECURITY
- **Exact scope:** Qualify research/model/news/import isolation and rights-aware exports
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-39, WP-43, WP-46
- **Contracts:** ModelRequest; InformationEvent; EvidenceRef; UiCommand
- **Likely modules:** tests/Security/; src/AutoTrade.Host/Export/
- **Dependencies:** WP-39; WP-43; WP-46
- **Reuse sources:** OS isolation; non-executable artifact formats
- **Tests:** Adversarial document instructions; unsafe artifact; cross-role retrieval
- **Acceptance:** Untrusted content cannot expand tools/authority or disclose credentials; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through tests/Security/ and its contract/qualification evidence.
- **Forbidden scope:** Executing arbitrary serialized models in financial host; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: SECURITY / untrusted-input-boundaries; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-53 — nvda-qualification

- **Authority family:** ACCESSIBILITY
- **Exact scope:** Run real Windows/NVDA and browser keyboard acceptance against built UI
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-44, WP-45
- **Contracts:** Document-07 workflow scripts; UiSnapshot
- **Likely modules:** qualification/nvda/; tests/Windows/Accessibility/
- **Dependencies:** WP-44; WP-45
- **Reuse sources:** NVDA and UI Automation inspection
- **Tests:** Confirmation, autonomy, emergency, disconnect, learning, copy/export, update
- **Acceptance:** Independent keyboard/NVDA evidence covers complete workflows and blocking defects resolved; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through qualification/nvda/ and its contract/qualification evidence.
- **Forbidden scope:** Automated accessibility scan presented as full NVDA proof; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: ACCESSIBILITY / nvda-qualification; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-54 — release-candidate

- **Authority family:** RELEASE
- **Exact scope:** Freeze exact signed host/web/desktop build and compatibility/evidence manifests
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-50, WP-51, WP-52, WP-53, WP-64
- **Contracts:** Release manifest; qualification matrix
- **Likely modules:** build/release/; docs/qualification/
- **Dependencies:** WP-50; WP-51; WP-52; WP-53; WP-64
- **Reuse sources:** Pinned build and SBOM tooling
- **Tests:** Artifact signatures/hashes; clean install; API compatibility; notices
- **Acceptance:** Reproducible RC has no unresolved blocking security/accessibility/package finding; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through build/release/ and its contract/qualification evidence.
- **Forbidden scope:** Floating dependencies or missing transitive license evidence; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: RELEASE / release-candidate; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-55 — whole-simulator-flow

- **Authority family:** INTEGRATION
- **Exact scope:** Integrate data→strategy→portfolio→risk→authority→execution→ledger→UI
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-21, WP-32, WP-33, WP-43, WP-44, WP-47
- **Contracts:** All core runtime contracts
- **Likely modules:** tests/Integration/WholeFlow/
- **Dependencies:** WP-21; WP-32; WP-33; WP-43; WP-44; WP-47
- **Reuse sources:** LEAN plus deterministic provider and actual host
- **Tests:** Confirmation/autonomous paths; partial/UNKNOWN; no-trade; restore
- **Acceptance:** A complete trace and accessible user workflow work with no live key or LLM; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through tests/Integration/WholeFlow/ and its contract/qualification evidence.
- **Forbidden scope:** Mocked-away risk/persistence presented as integrated product; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: INTEGRATION / whole-simulator-flow; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-56 — scientific-learning

- **Authority family:** QUALIFICATION
- **Exact scope:** Independently qualify causal research, candidate promotion and forgetting controls
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-36, WP-38, WP-42, WP-63
- **Contracts:** GateProfile; EvaluationResult; PromotionDecision
- **Likely modules:** docs/qualification/science/
- **Dependencies:** WP-36; WP-38; WP-42; WP-63
- **Reuse sources:** Independent statistical and causal fixtures
- **Tests:** Leakage sentinels; holdout misuse; retention regression; uncertainty
- **Acceptance:** Protocol/gates produce defensible pass/fail/inconclusive; economic claims match evidence; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through docs/qualification/science/ and its contract/qualification evidence.
- **Forbidden scope:** Promoting a visually good backtest despite invalid protocol; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: QUALIFICATION / scientific-learning; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-57 — forward-paper

- **Authority family:** QUALIFICATION
- **Exact scope:** Run frozen forward paper campaigns for every advertised provider/capability
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-22, WP-23, WP-24, WP-25, WP-26, WP-27, WP-40, WP-42, WP-48, WP-55, WP-61, WP-62, WP-65
- **Contracts:** Provider qualification; ExperimentProtocol; UiSnapshot
- **Likely modules:** docs/qualification/paper/
- **Dependencies:** WP-22; WP-23; WP-24; WP-25; WP-26; WP-27; WP-40; WP-42; WP-48; WP-55; WP-61; WP-62; WP-65
- **Reuse sources:** Official test/paper facilities plus stressed local execution model
- **Tests:** Sealed predictions; actual deadlines/costs; reconnect/manual activity
- **Acceptance:** Required forward statistical/operational evidence exists or explicit INCONCLUSIVE; no false edge claim; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through docs/qualification/paper/ and its contract/qualification evidence.
- **Forbidden scope:** Universal fixed duration/trade-count replacing registered power/coverage; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: QUALIFICATION / forward-paper; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-58 — bounded-real

- **Authority family:** QUALIFICATION
- **Exact scope:** Execute separately authorized bounded real-account qualification on exact build/policy
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-54, WP-56, WP-57
- **Contracts:** AuthorityPolicy; AdmissionRecord; provider report
- **Likely modules:** docs/qualification/live/
- **Dependencies:** WP-54; WP-56; WP-57
- **Reuse sources:** Qualified adapters and unchanged authority spine
- **Tests:** Real fills/fees; partials; reconcile; revocation and protection
- **Acceptance:** Actual account/build envelope reconciles and respects authority; unauthorized scope never traded; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through docs/qualification/live/ and its contract/qualification evidence.
- **Forbidden scope:** Architecture task authorizes real trades; withdrawals; unbounded live experiment; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: QUALIFICATION / bounded-real; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-59 — recovery-release

- **Authority family:** QUALIFICATION
- **Exact scope:** Independently qualify crash/outage/restore/upgrade and protection behavior
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-48, WP-49, WP-50, WP-57
- **Contracts:** Recovery matrix; release manifest; ReconciliationRun
- **Likely modules:** docs/qualification/recovery/
- **Dependencies:** WP-48; WP-49; WP-50; WP-57
- **Reuse sources:** Fault fixtures and clean-machine restore
- **Tests:** Power/network/storage/session loss; split-brain attempt; upgrade failure
- **Acceptance:** Recovery invariants and measured downtime/limits evidenced on delivered artifacts; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through docs/qualification/recovery/ and its contract/qualification evidence.
- **Forbidden scope:** Claiming external broker/funds recovery is guaranteed; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: QUALIFICATION / recovery-release; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-60 — whole-product-final

- **Authority family:** QUALIFICATION
- **Exact scope:** Audit all forty product sections and complete capability/evidence matrix
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-54, WP-56, WP-57, WP-58, WP-59, WP-61, WP-62, WP-64, WP-65
- **Contracts:** Product spec; all contracts; qualification matrix
- **Likely modules:** docs/qualification/final/
- **Dependencies:** WP-54; WP-56; WP-57; WP-58; WP-59; WP-61; WP-62; WP-64; WP-65
- **Reuse sources:** All integrated reusable components and evidence
- **Tests:** End-to-end acceptance, regression, exports, ongoing no-edge behavior
- **Acceptance:** No approved requirement silently dropped; engineering/economic claims separated; usable final release delivered; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through docs/qualification/final/ and its contract/qualification evidence.
- **Forbidden scope:** Calling one trade, green CI, source pass or a document set the finished program; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: QUALIFICATION / whole-product-final; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-61 — asset-provider-crosswalk

- **Authority family:** INTEGRATION
- **Exact scope:** Integrate futures/perpetual/options/corporate lifecycle with each advertising adapter
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-22, WP-23, WP-24, WP-25, WP-26, WP-27, WP-28, WP-29, WP-30, WP-31
- **Contracts:** InstrumentVersion; JournalTransaction; provider matrix
- **Likely modules:** tests/Integration/AssetProvider/
- **Dependencies:** WP-22; WP-23; WP-24; WP-25; WP-26; WP-27; WP-28; WP-29; WP-30; WP-31
- **Reuse sources:** Qualified lifecycle models and provider activity feeds
- **Tests:** Cross-currency fees; expiry; assignment; funding; adjusted contracts
- **Acceptance:** Every advertised provider×asset lifecycle combination has economic/reconciliation evidence; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through tests/Integration/AssetProvider/ and its contract/qualification evidence.
- **Forbidden scope:** Assuming all providers expose all assets or one test covers all combinations; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: INTEGRATION / asset-provider-crosswalk; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-62 — zero-model-economics

- **Authority family:** QUALIFICATION
- **Exact scope:** Qualify useful operation and cost reporting with all language models unavailable
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-39, WP-40, WP-55
- **Contracts:** DecisionProposal; ModelRequest; economic report
- **Likely modules:** docs/qualification/zero-model/
- **Dependencies:** WP-39; WP-40; WP-55
- **Reuse sources:** Deterministic baselines and actual cost budgets
- **Tests:** Remote outage; local resource exhaustion; small-capital minimums
- **Acceptance:** Research/paper/authority workflows remain useful and costs reconcile without LLM dependency; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through docs/qualification/zero-model/ and its contract/qualification evidence.
- **Forbidden scope:** Echo mock counted as a trading strategy; hidden paid fallback; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: QUALIFICATION / zero-model-economics; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-63 — source-agent-value

- **Authority family:** SCIENCE
- **Exact scope:** Qualify marginal contribution of sources/roles/models with causal ablations
- **Inputs:** 06_REPLAY_SCIENTIFIC_VALIDATION_ARCHITECTURE.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-11, WP-36, WP-40
- **Contracts:** InformationEvent; EvaluationResult; ModelResponse
- **Likely modules:** research/autotrade_research/evaluation/ablation/
- **Dependencies:** WP-11; WP-36; WP-40
- **Reuse sources:** Matched-input shadow comparisons
- **Tests:** Leave-source/agent-out; deadline parity; syndication duplicates; cost attribution
- **Acceptance:** Incremental value and uncertainty measured without future outcomes influencing routing; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through research/autotrade_research/evaluation/ablation/ and its contract/qualification evidence.
- **Forbidden scope:** Self-reported model quality or popularity used as evidence; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: SCIENCE / source-agent-value; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-64 — release-supply-chain

- **Authority family:** SECURITY
- **Exact scope:** Review exact release SBOM, provenance, dependency advisories and model/data rights
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-03, WP-46, WP-51, WP-52
- **Contracts:** Dependency/rights/release manifests
- **Likely modules:** docs/qualification/security/; provenance/
- **Dependencies:** WP-03; WP-46; WP-51; WP-52
- **Reuse sources:** Official license/advisory evidence and pinned dependency tooling
- **Tests:** Unlicensed import; changed hash; missing notice; vulnerable dependency policy
- **Acceptance:** No unreviewed blocking dependency/rights issue in exact distribution; residual risks explicit; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through docs/qualification/security/ and its contract/qualification evidence.
- **Forbidden scope:** Architecture-date license snapshot treated as permanent approval; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: SECURITY / release-supply-chain; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.


### WP-65 — runtime-resource-budget

- **Authority family:** PERFORMANCE
- **Exact scope:** Measure declared-load throughput/latency and research interference on target hosts
- **Inputs:** 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME.md; 02_CANONICAL_CONTRACTS.md; Exact accepted artifacts of WP-02, WP-09, WP-18, WP-40, WP-41, WP-47
- **Contracts:** MarketEvent; AdmissionRecord; job budget; metrics
- **Likely modules:** tests/Integration/Performance/; docs/qualification/performance/
- **Dependencies:** WP-02; WP-09; WP-18; WP-40; WP-41; WP-47
- **Reuse sources:** LEAN/runtime profiling and telemetry
- **Tests:** Burst ingest; slow disk; model contention; reconnection backlog
- **Acceptance:** Measured latency/staleness fit strategy horizons; overload degrades safely with no lost financial events; Evidence records exact source SHA, input/schema versions, tests actually run and unresolved limits.
- **Integration target:** Protected main through tests/Integration/Performance/ and its contract/qualification evidence.
- **Forbidden scope:** Unmeasured universal throughput or HFT claims; No unrelated shared-contract or sibling-provider mutation.
- **Conflicts:** Exclusive mutation key: PERFORMANCE / runtime-resource-budget; Shared schema or cross-owner changes require a separately owned contract migration; coordinate any overlapping module path.

