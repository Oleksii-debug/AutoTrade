# AutoTrade — owner-requested GitHub reuse integration wave (2026-10-17)

## Status and authority

Owner requested integration of all discussed useful third-party GitHub components.
This file is a concrete **supplement to Plans 1–10**, not a replacement for the
canonical roadmap or proof that upstream code is integrated, released or profitable.

**Audited inventory:** [control/external_reuse/catalog.json](../../control/external_reuse/catalog.json).
60 upstream repositories are recorded. This first code wave adds original,
optional research-only bridges for CCXT, River and Optuna. All other entries
remain NOT_IMPORTED. Zero upstream repositories are bulk copied in this wave.
No provider credential, paper/live authority, brokerage replacement or
economic-edge result is asserted.

### Plan 2 — Multi-agent / learning / model workers

1. Characterize Condor, TradingAgents and RD-Agent by exact version, role graph,
   memory behavior, prompts/tools, and source rights. Extract only narrowly
   compatible *permissively licensed* abstractions; do not install an opaque
   second coordinator into the financial host.
2. Route deterministic/ML/LLM specialists through the **existing** specialist
   DAG, immutable causal snapshots, budgets and evidence-bound aggregation.
3. Qualify the new River research-only bridge on *matured and reconciled*
   labels, changed regimes, drift and retention. Keep model version artifacts
   immutable and promotion controlled by the existing ChampionRegistry.
4. Qualify Optuna candidate searches under a fixed trial budget, protected
   holdout and scientific registry. An in-sample score is not a trading edge.
5. Evaluate optional Qlib, FinRL, FreqAI and similar engines in isolated
   Python research workers; no generated source in live financial authority.

### Plan 6 — Provider and data adapters

1. Evaluate CCXT public-only OHLCV bridge as one bounded ingestion option:
   official venue docs, allowed instruments, limits, explicit source times,
   rate restrictions, WebSocket/REST gap recovery, rights and provenance.
2. Compare already-present AutoTrade adapters against JKorf transport SDKs,
   Alpaca SDK and LEAN broker adapters. Prefer direct SDK dependencies,
   not copies or a second financial order sender.
3. Qualify each broker's client-order ID, partial fills, UNKNOWN, retries,
   cancellation, rate limits and durable reconciliation before activation.
4. No public market-data bridge or vendor SDK can confer a trading permission;
   no private API keys in model workers or checked-in config.

### Plan 1 — Financial core / strategy economics

1. LEAN stays selected canonical financial engine pending its embedding gate.
   NautilusTrader, vn.py, Backtrader, Jesse, Freqtrade, Hummingbot and
   similar alternatives may be inspected as independent reference/oracle
   sources; do not create a second ledger, OMS, or live dispatcher.
2. Port candidate strategies only as proposal plugins with explicit scope,
   tick/lot economics, slippage, funding/settlement and risk checks.

### Plan 4 — Security / licensing / provenance

1. For *each* candidate record exact upstream revision, source license text,
   notices, copyright, transitive component graph, and data/API terms.
2. MIT/BSD/Apache sources are not license-free; preserve notices and relevant
   modification terms. GPL/LGPL/AGPL may allow private use but require
   distribution/network-service analysis. Commons Clause is a restriction.
3. No copy from repositories lacking a clear grant of rights or from
   proprietary/pro-only code. Public AutoTrade repository cannot hide
   distributed copyleft material or secrets.
4. Pin approved dependency versions and audit vulnerabilities before enabling
   optional packages in the shipped build.

### Plan 7 — Scientific validation

1. Characterization tests for optional imports, missing packages and bad
   input. Run real dependency tests on Windows and Linux, not mocks alone.
2. Falsify future-label exposure, dirty data, duplicate candles, invalid
   decimals, model self-promotion and credential/trading authority escalation.
3. Prove model/strategy ablations versus frozen zero-LLM baselines, costed
   cash and buy/hold controls, out-of-sample/forward holdout, max drawdown.
4. Keep exact-SHA evidence and FAIL/INCONCLUSIVE outcomes. No ROI promise.

### Plan 10 — Historical and public-market simulation

1. Reuse canonical Plan10 PR #2389, public-source historical feed and
   existing simulation ledger/risk; do not implement a competing simulator.
2. Evaluate read-only CCXT snapshots only after source-rights and provenance
   evidence; no private provider account is a prerequisite.
3. Map research proposal events into historical causal timestamps, preserving
   next-bar fill assumptions, model/data hashes, labels and restart equivalence.
4. The optional bridges are *not wired into Plan10's active strategy yet*;
   do not mark Plan10 atomic sections DONE based on this isolated module.

### Plan 8 / 9 — product acceptance and release

1. Plan8 host/Web/Windows M1 must remain a single system; a third-party
   research adapter does not prove end-to-end UI, operator control or NVDA.
2. Plan9 external-provider and signed release gates remain independent.
   No third-party source bundle or model artifact gets a release claim until
   source composition, trust, Windows and licensing gates actually pass.

## Integration sequence and conflict policy

REUSE existing AutoTrade code -> CHARACTERIZE exact upstream commit/license ->
SELECT narrowly needed source or pinned dependency -> WRITE focused adapter ->
RUN offline negative tests -> RUN actual dependency cross-platform qualification
-> CONVERGE against current main and Plan10 PR #2389 -> security/CI review ->
merge only after legitimate gates -> exact-main readback -> advance status.

An item is counted as **implemented** only once it has real import/feature
code and verified tests in the integrated product scope. Documenting a repo,
installing an optional package, creating a PR or reading source alone does not
mean that entire upstream project has been added. Existing terminal DONE
rows are not reopened by this addendum without concrete regression evidence.
