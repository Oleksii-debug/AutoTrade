# AutoTrade — replay and scientific validation

Baseline 2026-09-22. Scientific claims are separate from software functionality. This document owns evaluation protocols, causality and promotion. Financial oracles are in document 04.

## 1. Three evidence layers

Layer A is blinded market replay with causal prices/liquidity and masked identity/calendar where economically valid. Layer B adds causally available news, macro/corporate information and professional knowledge permitted at that time. Layer C is forward paper on events unavailable when the candidate was selected. Each layer reports its own result; passing one cannot be silently substituted for another.

Historical success does not prove forward profitability. A pretrained model may recognize past events even after masking; record model training-cutoff uncertainty. Forward evidence is required for claims that depend on unfamiliar future events. Price-scale transforms are allowed only with a complete invariant proof for price, size, multiplier, ticks, strikes, currencies, funding, fees, liquidity and execution. Otherwise use original economics and identity masking only.

## 2. Causal runtime

A privileged data feeder owns the complete frozen dataset. The strategy/research process receives only observations whose evidenced availability is at or before the simulation clock. It cannot read full future files, final labels, future instrument universes or later registry results. Enforce this through process/filesystem/network permissions for untrusted generated strategies; an in-process API convention alone is insufficient.

Clock advancement: select next available event time → publish all events at that time in a stable tie-break order → update features and account events → evaluate scheduled decisions → admit simulated intents → process latency-aware venue events → commit episode checkpoint. Tie-break policy is declared and stable; sensitivity tests vary ambiguous same-time ordering. Decisions cannot react to a bar close before its final availability. An order submitted on an event cannot fill against earlier liquidity by default.

Separate event time, publication/availability time, ingest time and simulation clock. Historical ingested-at is not a substitute for historical available-at. If only day-level publication is known, use a conservative documented availability bound and label the dataset's resolution. Revisions, corrections and economic releases appear only at their historical release time. Feature fitting, imputation, scaling and universe selection occur inside each training fold, never on the combined future dataset.

Replay checkpoint includes simulation cursor, pending events, RNG states, strategy/model state, positions, orders, journal digest and model/data/config versions. Resume must equal uninterrupted replay within declared numerical tolerance. Genuine nondeterminism is recorded and repeated; do not manufacture exact reproducibility by hiding differing runs.

## 3. Execution simulation fidelity

Use LEAN fill/fee/slippage/calendar models as reusable interfaces, then qualify each asset/data fidelity combination. Tick/book mode can model latency, bid/ask, partial fills, volume/participation and queue uncertainty. Bar mode uses conservative bounds; ambiguous stop/limit ordering within one candle is pessimistic or reported as an interval. Do not assume both favorable extremes were reachable in the required order.

Include fees/rebates, minimum charges, spread, slippage, funding, borrow/financing, FX, corporate actions, expiry/assignment, halts, gaps, delistings and reject/cancel delays. Market-impact models are assumptions calibrated where possible, with stress multipliers and capacity analysis. Historical top-of-book without queue data cannot establish actual queue priority. Venue paper fills are one operational evidence stream, not a complete economic simulator.

Simulation model versions and calibration data are frozen per experiment. Use optimistic/base/adverse execution scenarios; a strategy that works only under optimistic fills cannot be promoted under a stricter registered rule. Simulate outages and unavailable exits as correlated market/operational events, not just independent random noise.

## 4. Protocol registration

Before accessing locked evaluation data register: hypothesis; strategy/model/features; allowed search space; training/validation/test/forward periods; labels and horizons; purge/embargo derived from overlapping information/labels; universe; cost/fill model; baselines; all primary/secondary metrics and directions; trial budget; stopping rules; statistical estimator; multiplicity treatment; minimum practical effect; risk constraints; retention tolerances; and promotion rule.

Train/dev may use rolling or expanding walk-forward splits. Purge samples whose feature/label windows overlap the evaluation information interval; embargo length follows the actual dependency horizon, not a decorative fixed percentage. Overlapping returns and market regimes invalidate naive independent-trade standard errors. Use suitable block/bootstrap or other dependence-aware estimators, with sensitivity to block choice. Failure to estimate uncertainty reliably is an INCONCLUSIVE result.

Baselines include cash/no-trade after operating cost, relevant passive or simple exposure-matched strategy, and the existing champion under identical data/cost conditions. Benchmark selection is pre-registered. Do not compare a leveraged candidate with an unleveraged baseline without exposure/risk context.

Every attempted trial, failed fit, discarded parameter set and data access is recorded. A consumed holdout becomes development evidence; it cannot remain labelled untouched after repeated selection. New validation requires a fresh untouched segment or an explicitly justified sequential/multiplicity-controlled procedure. [Deflated Sharpe research](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) informs a diagnostic for selection/non-normality; it is not a universal certification of edge.

## 5. Exact gate structure

Thresholds belong to a versioned `GateProfile` fixed before evaluation. The profile must contain numbers for risk/cost/retention limits and a statistical rule; an empty or modified-after-result profile fails qualification. There is no fixed daily return, trade count or mandatory holding duration.

| Gate | Required evidence | Pass / fail rule |
|---|---|---|
| G0 Reproducibility | Code/data/model/config/cost hashes, rights, environment lock, complete trial log | All required references resolve and hashes verify; unknown provenance fails |
| G1 Causality | Time/universe/revision/feature split tests and isolated replay audit | Zero unexplained leakage violations; unavailable timestamps cannot be invented |
| G2 Financial/operational validity | Document 04 oracles, execution fidelity, recovery/correction tests | All mandatory invariants pass; no economic result from an invalid accounting path |
| G3 Development robustness | Walk-forward/regime/cost/capacity sensitivity and baseline comparisons | Registered constraints hold; trial accounting complete; no cherry-picked replacement of primary metrics |
| G4 Locked evaluation | Untouched or valid sequential evaluation, uncertainty and multiplicity accounting | Registered rule passes; otherwise FAIL or INCONCLUSIVE, never discretionary PASS from a good chart |
| G5 Forward paper | New observations, real decision deadlines, actual compute costs, operational event capture | Required effect/risk/precision and operating stability pass under the same locked profile |
| G6 Deployment qualification | Account capabilities, recovery, accessible authority controls, valid user policy | Exact build/account/envelope is operationally qualified; no open blocking audit finding |
| G7 Bounded real evaluation | Separately authorized capital/risk envelope; actual fills and reconciliation | Continuous operational and economic monitoring; violations stop new exposure and invoke protection policy |

A usable default statistical profile for an initial economic claim: pre-register minimum practical net advantage δ over the selected baseline; require a one-sided 95% dependence-aware lower confidence bound greater than δ after the declared selection correction, while all risk/retention limits pass. δ includes incremental operating cost and deployment friction. Determine evidence length through a pre-registered power/precision analysis (for example 80% power for the chosen δ) and required regime coverage, not a universal number of days/trades. These percentages are a proposed protocol default, not a guarantee or a substitute for an appropriate estimator. A team may select another justified rule before seeing results.

## 6. Online adaptation and rollback

An approved online envelope specifies mutable parameters, allowed ranges, update frequency/resources, eligible labels, drift controls and stop conditions. Each update is recorded. Crossing the envelope creates a new candidate requiring gates again. A software code change, different feature availability, materially changed cost model or expanded authority is never disguised as a harmless online parameter update.

Retention tests cover protected prior regimes and adverse cases. Candidate promotion may be automatic after independent gates under user-authorized learning policy, but cannot change hard risk or trading permission. Promotion atomically changes future decision routing and retains the previous artifact. Rollback is a software/model routing action, not an automatic liquidation. Existing orders/positions are reconciled and managed under an explicitly compatible exit policy.

Monitor prediction calibration, feature/data drift, slippage versus model, realised costs, margin/risk violations and performance uncertainty. A short losing sequence is not by itself proof of failure; thresholds and stopping rules are pre-registered. An operational invariant failure is sufficient to halt new risk irrespective of recent profit.

## 7. Scientific test bank

- Shift every future event one step earlier and verify the leakage detector rejects it.
- Replace historical macro values with revised series and ensure vintage checks fail.
- Insert a delisted asset and verify the historical universe includes it.
- Fit a normalizer on the full dataset and ensure fold-provenance validation rejects it.
- Supply future labels/absolute dates through a tool or retrieval index and verify process/cutoff enforcement blocks them.
- Run an intentionally profitable impossible-fill strategy; the simulator must reject or price it adversely.
- Repeat a failed holdout with new parameters; the registry marks contamination and includes all trials.
- Hide losing trials or compute costs; evaluation completeness fails.
- Improve the newest regime while degrading a protected regime beyond tolerance; promotion fails.
- Restart at each replay checkpoint and compare events, ledger and decisions with uninterrupted execution.
- Test agent/source/model ablations on identical inputs and deadlines, including zero-LLM operation.
- Run shadow forward predictions sealed before outcomes, then score from later reconciled facts.

## 8. Evidence bundle and completion claims

Each qualified strategy bundle contains protocol, all trial references, dataset/rights manifests, source/feature lineage, simulator assumptions, metric tables with uncertainty, adverse scenarios, retention matrix, holdout-access record, forward sealed predictions, actual compute cost, independent review and exact deployable envelope. A report must state whether it establishes technical correctness, simulated performance, forward paper behavior or bounded real results. None is silently relabelled as the others.

The product is complete when it can perform this whole process, present honest results accessibly and operate within qualified authority. Whether a specific strategy has found durable economic edge remains an empirical conclusion that can be negative. No architecture can guarantee recovery of inaccessible external funds or future returns; it can prove local conservation, controlled uncertainty and correct response to observed failures.
