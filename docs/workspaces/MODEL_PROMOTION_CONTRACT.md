# AutoTrade — переносимість навчених агентів між SIMULATION і PROVIDER

**Owner rule dated 2026-10-15:** Full agent learning/strategy capabilities must be implemented now without broker accounts. Simulation and Provider are two operating environments of **one canonical agent/runtime**, not two agents with independent memories. This document specifies an engineering acceptance goal; it is NOT a claim that transfer is already implemented or verified.

## One canonical artifact contract

An exportable trained-agent package must retain at minimum:

1. Agent/model/strategy implementation IDs, versions, content hashes and exact code/dependency/source schema references.
2. Learned model parameters or weights, strategy hypotheses/policies, feature transforms/normalizers, input/output schemas, instrument and timeframe semantics; explicit cross-environment compatibility constraints.
3. Immutable closed-trade/outcome learning history, memory state, update count, training checkpoints, deterministic seeds, rehydration/restart verification and controlled migrations.
4. Training dataset manifest/source/checksums, exact as-of availability, training/validation/holdout windows, lookahead/leakage controls and research-only source-rights restrictions.
5. Risk policy and investment constraints, position/fee/slippage assumptions, capital units and precision; real provider policy can only tighten, never silently raise limits or inherit a simulation-only permission.
6. Portfolio/journal reconciliation checkpoints kept separate from agent learning memory. No simulated fills or synthetic cash can be promoted as actual broker settlement/account balance.
7. Versioned import/export schema, signature/hash checks, compatibility verdict and full provenance. Fail closed on version/feature/data mismatches, missing history, invalid hashes, attempted external activation or unsafe permission escalation.

## Verification before external promotion

- Freeze a trained model; save/restart/reimport it into a clean provider-mode environment with external order route **disabled**.
- Feed identical time-consistent observations (same features, asset semantics and frozen policy) to both environments; compare deterministic strategy decisions where applicable.
- Independently qualify provider market feed latency, data revisions, calendars, bid/ask conventions, lot constraints, order execution, fees, cash settlement, borrow/margin, slippage and reconciliation. Behavioral equivalence of decisions does NOT assert equivalent fills/returns.
- Report out-of-distribution, regime, asset/venue changes, feature drift, rights/license restrictions and holdout results; FAIL/INCONCLUSIVE is acceptable.
- Until distinct real-trading permission is given, imported agents must remain `TRADING_AUTHORITY=NOT_AUTHORIZED` and `REAL_MONEY=NOT_AUTHORIZED`.

## Architecture rule

Exactly one implementation of learning, policy, shared state, risk, OMS and finance. Swappable market-data/adapters and execution providers supply facts under a common protocol. No separately developed dummy agent or disposable memory under Simulation AutoTrade. Keep the business/data-research rights boundary separate from signed commercial release rights.

## Evidence and completion

Both workspaces link to one frozen artifact identity and CI/evaluation receipts. Mark "portable learning" DONE only after real save/reload/migration tests and identical-observation decision parity under the verified shared contract. Never mark provider-qualified, NVDA-physically-verified, profitable, signed-release or live-trading DONE from these source-only tests.
