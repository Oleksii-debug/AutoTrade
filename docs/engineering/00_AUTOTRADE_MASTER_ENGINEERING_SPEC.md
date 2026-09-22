# AutoTrade — master engineering specification

Engineering baseline: 2026-09-22. Owner: Oleksii. Objective: TIME_TO_WHOLE_FINISHED_AUTOTRADE.

## Publication status

The engineering package contains core documents 00–09, the machine-readable bank of 65 work packages, and optional development-operation documents 10–13 prepared after the core. This master is the entry point. The approved product document has been updated in place with twelve targeted changes. The package is an implementation baseline with explicit empirical qualification gates; it is not an implemented trading system. A ZIP and a derived accessible HTML reading edition accompany the canonical Markdown documents.

The approved product source remains `AutoTrade — Final Product Specification (EN).docx` in the parent folder. All forty sections remain in scope. Research was performed on 2026-09-21–22. Existing repositories were reviewed at explicit commits; their test suites were read selectively, not executed. Since publication of the Drive baseline, the AutoTrade GitHub repository has been bootstrapped, canonical control issues were created, and a narrow set of neutral first-party research primitives was migrated with focused local tests. This does not imply completion of the financial runtime or product qualification.

## Binding clarification from the owner

Provider selection and architecture MUST NOT depend on an assumption that the owner registers in Ukraine or Slovakia. Both are possible in the owner's context. All six requested providers remain in scope. Account connection discovers the actual entity, endpoint, permissions, product capabilities and instrument restrictions. These are runtime facts; they are not a country-based design filter or a reason to remove a provider from the product.

## 1. System outcome

AutoTrade is an accessible, provider-independent, multi-asset research, learning, decision, portfolio, risk and execution system. It supports historical causal replay, simulation, forward paper and explicitly authorized real trading through official APIs. It handles spot, margin, long/short, equities, futures, perpetuals and options, including their lifecycle events. It remains useful with deterministic strategies and zero language-model calls. Models may improve research and decisions but never acquire financial authority from their own output.

One final product is being built. Intermediate integration slices prove parts of that product; they are not smaller substitute products. A technically complete system may correctly choose not to trade when economic evidence is insufficient. No target return, win rate or guaranteed profitability is asserted.

## 2. Foundation decision

Use QuantConnect LEAN as the preferred reusable financial-engine foundation. Follow-up source verification established that `985ef30ad3ac774218c5ac516b4cb0aa2655730f` is a LEAN commit dated 2026-09-18 (tree `4b163abf9fca60e731b76510b9ae6721ffff7e6c`). WP-02 must pin the exact selected commit and review any later delta before adoption. The source is Apache-2.0 and covers orders, portfolios, buying power, instruments, corporate actions, calendars, fills and backtesting. Build an AutoTrade host and guarded brokerage integration around it. Avoid duplicating an entire OMS, portfolio engine or backtester.

This is an architecture selection, not a completed adoption benchmark. The first implementation gate must prove embedding, packaging, decimal behavior, event ordering, restart/reconciliation and adapter isolation. If this gate exposes an unfixable requirement mismatch, the recorded alternate is NautilusTrader; do not develop two authoritative engines in parallel. A switch requires a requirement-by-requirement evidence comparison, including migration and licensing cost.

Use a thin pinned upstream integration rather than an uncontrolled LEAN fork. Own changes belong in AutoTrade adapters and policy boundaries; unavoidable upstream patches have a small manifest and upstream issue reference. Upstream API changes enter only through a reviewed dependency update.

## 3. Technology and deployment

| Concern | Decision | Reason and boundary |
|---|---|---|
| Financial runtime | C# / .NET 10, LEAN integration | Matches the reviewed LEAN project target; decimal money and existing multi-asset engine |
| Control API | ASP.NET Core in the runtime host | One command authority, authentication and state projection |
| Research and learning | Isolated Python 3.12 workers | Scientific ecosystem without arbitrary Python inside the authority process |
| Desktop | Windows 11 WPF shell with WebView2 plus native emergency/status surface | Accessible shared web workflows, clear host identity and independent stop access |
| Web | TypeScript / React, semantic HTML | Same API and event stream as desktop; keyboard and NVDA acceptance required |
| Durable authority | SQLite WAL, FULL synchronous, one writer, local filesystem | Transactions across intent, reservation, outbox and audit state |
| Historical/research data | Immutable Parquet plus DuckDB query workers | Efficient columnar reuse; no authority state in analytical tables |
| Artifact storage | Content-addressed files with manifests | Reproducible datasets, models, experiments and exports |
| Secrets | Windows credential protection locally; host secret store remotely | Never in model prompts, memory, journal payloads or logs |
| Operations | Structured events and OpenTelemetry-compatible traces/metrics | Reconstruct decisions without logging secrets |

These are compatibility baselines, not permission to install floating latest versions. Bootstrap resolves exact versions and transitive licenses into locks and an SBOM. Native Windows packaging is a qualification deliverable. Local operation needs no Docker, Kubernetes, Kafka or mandatory paid cloud service.

The host can run on the PC or an explicitly configured always-on machine. Closing a UI does not close the host. A powered-off PC cannot run trading. There is one active execution owner per account/environment. The initial topology uses one host; remote standby cannot promote by merely timing out a lease. A fenced ownership transfer must prove the old sender cannot submit.

## 4. Boundaries and flow

1. Provider/data adapters ingest raw observations and account events with source identity and observation times.
2. Normalization publishes versioned instruments and append-only events; bad or stale input is quarantined.
3. Research and strategy workers receive only causally available views and emit proposals.
4. Portfolio construction converts proposals into target exposure and executable intent candidates.
5. Independent risk checks portfolio-wide current exposure, reservations, unknown orders, costs, leverage and capability evidence.
6. Authority checks confirmation or autonomous policy, then commits the intent, reservation and outbox atomically.
7. A guarded LEAN brokerage boundary verifies the current policy/fence at the last submission point and submits through the selected official API adapter.
8. Execution reports and reconciliation establish actual fills and balances. Acknowledgement alone never establishes a fill.
9. Durable projections feed both interfaces, economic reporting and immutable learning episodes.
10. Independent evaluation may promote a candidate model or strategy within allowed policies. It cannot enlarge user authority or hard risk limits.

## 5. Financial authority and truth

The provider supplies external account facts. The AutoTrade journal supplies the immutable local history of requests, observations, reservations, corrections and decisions. LEAN runtime state is derived execution/portfolio state and must reconcile with the journal and provider before enabling new risk. Conflicting facts remain explicit exceptions; no component silently overwrites the other to make totals agree.

Exactly-once external execution cannot be assumed over a network. Use durable intents, stable provider-compatible client IDs, bounded submission attempts, idempotent observation processing and reconciliation. A timeout after send becomes UNKNOWN, retains risk and cannot cause a blind retry. A missing order in a short recent-order page is not evidence that it never existed.

Money and quantity travel as canonical decimal strings with currency/unit metadata. Prices are instrument-versioned. Floating point is permitted for statistical estimates, never as the authoritative cash or fill representation. Reservations consider pending cancel/replace, manual orders, partial fills and fees. Corrections reverse earlier journal postings and append corrected facts.

## 6. Learning and scientific authority

Separate immutable experience, mutable retrieval indexes, model artifacts, experiment records and approved policies. Retaining old records is not proof of avoiding catastrophic forgetting. Compare candidate behavior on protected past-regime suites, recent forward data and stress scenarios. Replay and bounded online adaptation are selected techniques; stronger or larger models are not assumed superior.

A candidate progresses through registered hypothesis, causality checks, development evaluation, locked out-of-sample evaluation, forward paper, operational qualification and a bounded authorized live envelope. Every gate records exact dataset/model/code/config/cost hashes and the number of tried candidates. A failed economic gate blocks promotion even if all software tests pass.

Blinding removes identifying context only where it preserves economics. Price rescaling cannot independently ignore lot size, multiplier, ticks, strikes, currency and fees. Pretrained historical knowledge remains a contamination risk; only genuinely later observations can resolve the corresponding forward-evidence requirement.

## 7. Product workflows

The user connects an account, inspects discovered capabilities and host identity, imports or subscribes to lawful data, runs research/replay, reviews evidence, activates paper operation, inspects portfolio and reasons, configures exact authority, and can revoke it or stop new exposure from any interface. Every workflow is keyboard complete. Critical values are text, selectable and copyable; charts supplement accessible tables.

Confirmation binds a specific action and expiry. Autonomous mode binds a versioned policy envelope. Blocking new exposure, cancelling orders and flattening positions are separate commands with separate outcomes. Existing protective orders must not be accidentally cancelled by a generic stop. Provider-native protection and its limitations remain visible when the host is disconnected.

## 8. Non-negotiable acceptance

- Financial conservation and P&L attribution across fills, fees, funding, FX, corporate actions, settlement and corrections.
- Causal replay; no future observations, revised data leakage or survivorship substitution.
- Recovery after every send/commit boundary, duplicate event, partial fill, disconnect and corrupt/full disk condition.
- All six provider contracts qualified independently for the capabilities they actually expose to a connected account.
- Complete asset lifecycle coverage or an explicit unavailable capability until qualification; no silent partial support.
- Meaningful zero-LLM operation, cost-bounded routing and independently controlled learning promotion.
- Same authoritative state and command semantics on desktop and web.
- Signed Windows installation/update/rollback and real keyboard/NVDA testing, including disconnected/emergency states.
- Reproducible release provenance, dependency/license review, secret isolation, export and restore.
- Separate technical completion and statistical/economic evidence; absence of edge is an honest supported result.

## 9. Canonical package map

| File | Owns |
|---|---|
| 00_AUTOTRADE_MASTER_ENGINEERING_SPEC | Decisions, scope, authority and navigation |
| 01_REUSE_AND_OPEN_SOURCE_MASTER_MAP | Source audit, evidence, licenses and migration |
| 02_CANONICAL_CONTRACTS | Types, envelopes, interfaces, invariants and transactions |
| 03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE | Data and six provider integrations, order lifecycle and reconciliation |
| 04_PORTFOLIO_RISK_ECONOMIC_ARCHITECTURE | Money, portfolio, margin, sizing, lifecycle and economic oracles |
| 05_LEARNING_MEMORY_AI_AND_AGENT_ARCHITECTURE | Strategies, learning, memory, agents and compute routing |
| 06_REPLAY_SCIENTIFIC_VALIDATION_ARCHITECTURE | Causality, experiments, promotion and qualification |
| 07_DESKTOP_WEB_ACCESSIBILITY_AND_RUNTIME | UX, host/security/operations/recovery/release |
| 08_DELIVERY_DEPENDENCY_AND_WORK_PACKAGE_PLAN | Dependency stages and complete work-package bank |
| 09_REPOSITORY_INITIALIZATION_BLUEPRINT | Initial imports, structure, contracts, CI and ownership |
| 10_SWARM_DEVELOPMENT_OPERATING_MODEL | Optional semantic ownership, self-dispatch and independent audit |
| 11_GITHUB_CONTROL_PLANE_AND_PARALLEL_DELIVERY | Optional atomic registry, PR/integration and renewable task state |
| 12_UNIVERSAL_WORKER_PROMPT | Stable future implementation-worker instruction |
| 13_UNIVERSAL_AUDITOR_PROMPT | Stable future independent-auditor instruction |

Each subject has one canonical owner above. Companion documents expand this master; they cannot silently change it. Conflicting contract changes require an explicit decision record and migration plan before implementation.

## 10. Evidence and limits

Primary sources include [LEAN](https://github.com/QuantConnect/Lean), [NautilusTrader](https://github.com/nautechsystems/nautilus_trader), [Autosport](https://github.com/Oleksii-debug/Autosport), [Nika-Core](https://github.com/Oleksii-debug/Nika-Core), and the official API documentation of all six providers. The reuse map records exact audit commits, inspected modules and adoption risks. Current research does not constitute a compiled integration, live broker test, license opinion or economic validation. Those are explicit implementation work packages rather than hidden assumptions.

## 11. Product review change record

All forty numbered product sections and their original scope were retained. Two original paragraphs were replaced and ten paragraphs added. Changes: acknowledgement distinguished from fills (§7); coherent economic blinding and pretrained-history limits (§17); account-discovered capabilities without country assumptions (§6); complete instrument lifecycle (§8); historical availability/vintages/rights (§14); bounded learning and actual retention tests (§16); exact confirmation/revocation (§23); emergency/UNKNOWN/recovery semantics (§27); active host and single execution owner (§31); trading versus operating economics (§32); untrusted information and credential authority (§33); separate technical/economic qualification (§37). The original Drive file ID is unchanged. This package's DOCX is a snapshot of that canonical file, not a competing product specification.

## 12. Product-to-engineering traceability

| Product section | Engineering owner | Implementation/acceptance packages |
|---|---|---|
| 1 Status and purpose | 00, 09 | WP-01, WP-60 |
| 2 Product vision | 00, 05 | WP-40, WP-55, WP-60 |
| 3 User receives | 07 | WP-43–45, WP-53 |
| 4 Universal market | 03, 04 | WP-07, WP-28–31, WP-61 |
| 5 Multi-provider | 03 | WP-08, WP-22–27 |
| 6 Capabilities | 02, 03 | WP-07–08 |
| 7 API trading | 02, 03 | WP-18–27 |
| 8 Trading capabilities | 03, 04 | WP-19, WP-28–31, WP-61 |
| 9 Live event stream | 02, 03 | WP-09, WP-47, WP-65 |
| 10 Autonomous selection | 04, 05 | WP-32–34, WP-40 |
| 11 Thesis/instrument distinction | 02, 04 | WP-30, WP-32 |
| 12 Agents | 05 | WP-39–40, WP-63 |
| 13 Information intelligence | 03, 05 | WP-11, WP-37, WP-63 |
| 14 Historical world | 03, 06 | WP-10, WP-12–13 |
| 15 Learning waves | 05, 06 | WP-35–38, WP-41–42 |
| 16 Adaptation speeds | 05 | WP-38, WP-42 |
| 17 Blinding | 06 | WP-12, WP-36, WP-56 |
| 18 Historical news | 05, 06 | WP-11–12, WP-63 |
| 19 Evaluation layers | 06 | WP-36, WP-56–58 |
| 20 Learning across regimes | 05, 06 | WP-34, WP-38, WP-56 |
| 21 Strategy independence | 05 | WP-33–34, WP-36 |
| 22 Paper before real | 03, 06 | WP-21, WP-57–58 |
| 23 Authority policies | 02, 07 | WP-17–18, WP-43, WP-53 |
| 24 Independent risk | 04 | WP-15–16 |
| 25 Portfolio | 04 | WP-14–16, WP-32 |
| 26 Execution truth | 02, 03, 04 | WP-18–20 |
| 27 Recovery | 03, 07 | WP-20, WP-48–49, WP-59 |
| 28 Accessible interfaces | 07 | WP-43–45, WP-53 |
| 29 Explanations | 02, 05, 07 | WP-37, WP-43–44, WP-47 |
| 30 Contribution measurement | 05, 06 | WP-36, WP-39, WP-63 |
| 31 Modes/background work | 05, 07 | WP-41, WP-45, WP-48, WP-65 |
| 32 Economics/capital | 04 | WP-14–16, WP-32, WP-62 |
| 33 Credentials/permissions | 02, 07 | WP-17, WP-46, WP-52, WP-64 |
| 34 First-party foundations | 01, 09 | WP-03–04 |
| 35 Full live cycle | 00, 03–07 | WP-55, WP-57–60 |
| 36 Historical learning cycle | 05, 06 | WP-12–13, WP-35–42, WP-56 |
| 37 Finished | 07, 08 | WP-53–60 |
| 38 Truthfulness | 00, 06, 08 | WP-36, WP-56–60 |
| 39 Engineering decisions | 00–09 | WP-01–65 |
| 40 Final product formula | 00, 08 | WP-60 |

## 13. Decisions versus empirical gates

The chosen boundaries, data/financial/provider/learning/UI contracts, technology direction and dependency plan are specified here. Remaining empirical work has a named owner: LEAN embedding/performance (WP-02/65); full dependency/source-rights clearance (WP-03/04/64); exact provider/account/API behavior (WP-22–27/61); simulator calibration and economic edge (WP-13/36/56–58); actual Windows/NVDA usability (WP-50/53); recovery on delivered builds (WP-48/49/59). These gates require builds, credentials, hardware or forward observations unavailable to a documentation-only task. No claim that they already passed is made.

Research is strongest for the inspected engine/first-party modules and official API semantics. Infrastructure shortlist items and partially inspected modules remain explicitly unverified in document 01. Recheck only those exact gaps before adoption; do not restart the architecture project or import uncertain code on an assumption.
