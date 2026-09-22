# AutoTrade — repository initialization blueprint

Repository: `Oleksii-debug/AutoTrade`. It now exists and bootstrap migration has started. The earlier recommendation was to keep it private while rights, credentials handling and distribution composition are qualified; current repository visibility must be treated as live configuration rather than inferred from this historical recommendation. Visibility never exempts dependency-license obligations.

## 1. Start from a specific reusable foundation

Start from a pinned LEAN integration, not an empty financial-engine implementation and not a copy of the entire Autosport product. Follow-up verification established `985ef30ad3ac774218c5ac516b4cb0aa2655730f` as a LEAN commit dated 2026-09-18. WP-02 must record the exact selected commit/build artifact and explicitly review any later delta before updating the pin. Use a submodule or reproducibly fetched source dependency behind a small integration project until upstream packaging is validated; record the exact artifact/source digest. A submodule must pin a commit, not a moving branch. Do not fork unless a demonstrated integration requirement needs a patch.

The repository itself contains AutoTrade contracts, host/policy integration, adapters, research, accessible UI, tests and evidence. LEAN's own license/notices remain with its source/binaries. First-party A/B imports occur only after provenance approval and characterization. No runtime dependency on Autosport or Nika is permitted merely to save extraction work.

## 2. Initial structure and semantic ownership

| Path | Responsibility / owner |
|---|---|
| `README.md`, `AGENTS.md`, `SECURITY.md`, `LICENSE`, `NOTICE` | Product goal, working rules, private disclosure process and chosen distribution terms |
| `docs/engineering/00..09*.md` | This engineering baseline, versioned in repo after initialization |
| `docs/adr/`, `docs/qualification/` | Decisions/migrations and evidence conventions |
| `contracts/jsonschema/`, `contracts/openapi/`, `contracts/fixtures/` | Contract authority; versioned schemas and language-neutral examples |
| `src/AutoTrade.Contracts/` | Generated C# types and strict serialization boundary |
| `src/AutoTrade.Domain/` | Neutral identifiers, command/event semantics, policy types |
| `src/AutoTrade.Persistence/` | SQLite migrations, journal, outbox, snapshots and transactional repositories |
| `src/AutoTrade.Engine.Lean/` | LEAN embedding, engine event translation, guarded brokerage boundary |
| `src/AutoTrade.Execution/` | Admission/dispatch, order projection, reconciliation and owner fences |
| `src/AutoTrade.Portfolio/`, `src/AutoTrade.Risk/` | Economic projections, reservations and independent risk |
| `src/AutoTrade.Data/`, `src/AutoTrade.Information/` | Normalized data, manifests, causal availability and information claims |
| `src/AutoTrade.Providers.Abstractions/` | Provider contracts and qualification helpers |
| `src/AutoTrade.Providers.{Bybit,Kraken,WhiteBIT,Binance,IBKR,Alpaca}/` | Separate provider ownership; shared contracts only |
| `src/AutoTrade.Science/`, `src/AutoTrade.Learning/` | Protocol/promotion authority and learning orchestration |
| `src/AutoTrade.ModelGateway/`, `src/AutoTrade.Jobs/` | Model routing, privacy/cost budgets and durable research jobs |
| `src/AutoTrade.Host/` | ASP.NET Core composition/auth/API/health; no duplicate domain logic |
| `src/AutoTrade.Desktop/` | WPF/WebView2 shell, native emergency/status and installer integration |
| `research/autotrade_research/{io,artifacts,data,features,strategies,learning,evaluation}/` | Isolated Python scientific implementation |
| `web/src/{api,components,workflows,accessibility}/` | Shared semantic UI and generated client |
| `tests/{Contracts,Finance,Execution,Recovery,Providers,Science,Integration,Windows}/` | Independent test families and approved fixtures |
| `research/tests/`, `web/tests/`, `qualification/nvda/` | Python, web and real NVDA scripts/evidence |
| `third_party/`, `provenance/`, `licenses/` | Pinned foundations, import manifest, retained license texts/SBOM |
| `build/`, `packaging/windows/`, `deploy/` | Reproducible build, signed installer and optional host deployment |
| `.github/workflows/`, `.github/CODEOWNERS` | CI and semantic review ownership |
| `control/` | Optional later machine-readable delivery/claim state; never runtime financial state |

The braces above denote separate actual directories to create, not literal filenames. Avoid a generic “utils” module accumulating financial authority. Generated types are changed through schemas; provider adapters cannot independently redefine shared money/order contracts.

## 3. First commit sequence

1. Repository purpose, approved product/engineering baseline, licensing decision record, dependency/provenance policy, AGENTS working rules and secret-safe ignore rules. No real credential/data dumps.
2. Canonical schemas, OpenAPI, event versioning, financial/causal/authority fixture corpus and generated-binding workflow. Commit architectural tests before implementation abstractions proliferate.
3. Exact LEAN source/build pin, notices and minimal embedding/adoption harness. Establish .NET/Windows build and future API integration feasibility before new engine code.
4. Persistence schema/migration and financial admission/outbox/reconciliation harness; crash points and independent money vectors.
5. Provider abstraction plus deterministic simulated provider and contract harness. Provider-specific workers branch from stable contracts and build independently.
6. Provenance-cleared neutral first-party utilities into research/artifact paths, retaining original characterization tests and new domain-neutral fixtures.
7. Accessible shared UI skeleton over real versioned state; zero-LLM strategy/replay path; durable learning/evaluation job interfaces.
8. Remaining dependent work packages from document 08. Integrate vertical slices continuously while full provider/asset/science/UI scope proceeds in parallel.

The order denotes dependency priority, not a ban on parallel work on independent contracts, fixture design or UI semantics. Do not wait for all six adapters before exercising the complete pipeline through the simulator.

## 4. Never import

Sports matches/teams/bookmaker odds, ticket win/loss/void settlement, sports-specific risk metrics, browser trading automation, generic retry-on-write wrappers, unreviewed executable model serialization, hidden credential configuration, a second authoritative OMS, or unfinished first-party application orchestration. Do not implement a new backtester/indicator library merely because the reusable engine exposes a different interface.

## 5. Branch, review and CI strategy

Protected `main` is always buildable. Use short-lived `wp/<id>/<semantic-slug>` branches and one principal semantic responsibility per PR. Draft PRs may establish dependent work, but merge only after required contracts and exact-head evidence pass. Stacked PRs record base dependencies; no duplicate independent implementation of the same work package. Resolve broad changes through a small reviewed contract migration followed by compatible implementation patches.

CI files: `contracts.yml`, `dotnet.yml`, `python.yml`, `web.yml`, `finance-recovery.yml`, `provider-contracts.yml`, `security-license.yml`, `windows-package.yml`, `qualification.yml`. Package/service boundaries determine test selection; shared-contract and journal changes trigger the relevant cross-language/cross-component matrix. Tests from untrusted contributions receive no live provider or signing secrets. Release signing is isolated from PR execution.

Lock files: .NET SDK `global.json` plus NuGet lock/central package versions; Python exact environment lock; frontend package lock; LEAN/dependency manifest; model/data manifests when used. CI compares generated bindings and refuses uncommitted drift. Artifact filenames and reports include exact source SHA/schema/dependency manifest digest.

## 6. Ownership and integration

Contract owners own schema meaning; financial owners own postings/invariants; execution owners own send/reconcile; each provider owner owns only provider translation; science owners own experimental validity/promotion; UI owners own semantic workflows; release owners own build/upgrade. Code review ownership does not grant live trading permission.

Integration uses the same simulator and canonical fixtures from every workstream. A provider may add an extension payload only through a versioned namespaced schema with defined fallback; it cannot smuggle new economic meaning through an untyped dictionary. Shared contract changes declare affected consumers, compatibility period, fixture migration and rollback. Continuous integration is preferred; coordinated waves are needed only for actual incompatible schema/engine changes.

## 7. Initialization acceptance

A clean clone on Windows and Linux builds the selected host/research tests from locked dependencies; Windows additionally packages the desktop shell. No private machine paths, hidden sitecustomize/import shadowing or undocumented runtime downloads are required. Simulator-only operation produces a reconstructed decision→risk→intent→fill→ledger→UI trace with zero LLM calls. License/provenance manifests are complete for imported source. Financial and causality fixtures fail when intentionally violated. No real trading credential is needed to demonstrate initialization acceptance.
