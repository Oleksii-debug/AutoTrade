# Симуляційний AutoTrade — канонічний робочий підпроєкт

**Власник:** AutoTrade. **Спільний репозиторій:** `Oleksii-debug/AutoTrade`. **Поточна реалізація:** Plan 10 / 23 atomic sections, status by `MULTI_PLAN_CLOSURE_STATE.md`, not by this text.

**Google Drive:** https://drive.google.com/drive/folders/1qaWZPFEy1oWghv1nVID2SXjBwDBhfYQ6
**Канонічний план:** https://docs.google.com/document/d/1bTDCb0yGLrOqlKClhyAD_7k7aZsP57kFRkexHz_b6d4/edit

## Meaning of simulation

This is a **genuine autonomous trading agent and learner**, not a fake/test-only agent. **SIMULATION** labels the *market execution/environment*, not the quality or persistence of strategy, memory, model, portfolio policy, agent decision logic, or runtime. Exchange/order effects use the canonical simulated provider until the separate provider project has independently qualified live bindings. Do not route real money or credentials.

## Own scope

- Real, provenance-verified historical public data, as-of chronological feed, future-data exclusion, exact restarts, bounded continuous updates within source rights.
- Canonical agent(s) generating BUY, SELL, HOLD, NO_TRADE with explicit reason and timestamps; canonical OMS/risk/cash/ledger, simulated order execution *after* allowed decision time with fees, spread, slippage, lot sizes and rejection/partial fills (honest OHLCV limitations).
- Separate real strategy-learning loop, immutable completed-outcome memory, versioned model/weights/checkpoints, strategy registry, reproducible upgrade steps and stable recoverable state.
- Virtual starting balances **USD 200** and **USD 1000** as two isolated experiment configs; owner can select amounts. No actual financial spend.
- Time-separated training/validation/untouched holdout, cash / buy-hold baselines, costs/drawdown, negative controls, honest FAIL or INCONCLUSIVE outcomes; performance never inferred from compiler passes.
- User-operable Windows + canonical Web accessibility-first interface: install/start/pause/resume/stop, dataset/date/symbol/strategy/budget, event timeline, portfolio and learning changes, text export and NVDA-friendly semantic output.
- Delivered **INTERNAL / SIMULATION / UNSIGNED** installable or portable Windows ZIP with checksums, instructions and accessible diagnostics; no implied commercial/signature/physical NVDA acceptance.

## Exclusions moved to Provider AutoTrade

Account onboarding, broker credentials/API keys, real execution routing, PAPER/LIVE permission, external reconciliation with a financial institution, live-money approval, final public signed release and physical NVDA evidence for external release. None is a gating prerequisite for internal functional Simulation AutoTrade. Do not remove core security gates or claim absent external facts as PASS.

## Integration and DONE policy

Reuse accepted Plans 1–8. Reuse/repair/converge open Plan10 PR **#2389** before duplicates. Respect independent protected-control PR **#2388** and exact-head authorization; never bypass protection or skip known failures. Execute each of the 23 current atomic sections: real tests/negative/recovery, exact-SHA evidence, valid CI, safe merge to `main`, readback, *then* DONE; continue to the next unfinished actionable section. The older ten aggregates are not independent DONE.

Promotion to Provider AutoTrade must satisfy [MODEL_PROMOTION_CONTRACT.md](../MODEL_PROMOTION_CONTRACT.md); the same agent, models and durable strategy memory must be reusable, subject to compatibility tests and external execution qualification.
