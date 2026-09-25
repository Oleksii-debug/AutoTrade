# AutoTrade — portfolio, risk and economic truth

Baseline 2026-09-22. This document defines system accounting and decision design, not an investment recommendation. LEAN supplies reusable engine mechanisms; the AutoTrade journal, provider reconciliation and independent fixtures determine whether they match the required economics.

## 1. Accounts, books and valuation

Maintain account/environment-separated cash by currency, settled/unsettled buckets, inventory by instrument/version and lot, liabilities, reserved resources, derivative collateral, accrued fees/interest/funding, realized P&L, unrealized P&L and external cash flows. Broker-reported equity and local calculated equity are separate fields until reconciled. Multi-provider aggregation never implies capital can be transferred instantly or used twice.

Choose an account reporting currency and a portfolio reporting currency. Each conversion records FX source, timestamp and side/conservative haircut. Missing or stale FX makes valuation uncertain and prevents unsupported capital allocation. Marked equity uses tradable/conservative marks appropriate to asset class; last trade is not always a realizable liquidation price. Report both mark-to-market and stressed liquidation estimates.

Authoritative postings use exact decimal arithmetic. Quantize order quantity to an allowed step without increasing risk; round limit/stop prices according to side, desired constraint and provider rules, then re-evaluate risk. Never hide rounding loss. Unsupported precision/range rejects the instrument/action explicitly. Fees can be negative rebates and denominated in base, quote or a third currency.

## 2. Invariants

1. Every provider fill produces exactly one economic posting set unless corrected by an explicit reversal/replacement.
2. Cash and instrument-unit ledgers each conserve their own units through balanced postings. Currency conversion uses clearing entries and an evidenced rate; unlike currencies are never directly summed.
3. Current holdings = prior holdings + executions + corporate/lifecycle events + external adjustments, all evidenced.
4. Equity change = investment/trading P&L + external net flows, under an explicit valuation basis; deposits are not strategy profit.
5. Available capital excludes reservations, settlement restrictions, liabilities, protective liquidity buffers and plausible UNKNOWN exposure.
6. Pending cancel is still working risk. Filled and unfilled portions cannot both reserve the full original notional without an explicit conservative overlap reason, and neither may disappear.
7. Portfolio and provider balances reconcile within declared currency/instrument tolerances; tolerance is not a bucket for unexplained losses.
8. Leverage uses gross and net exposure with derivative-specific equivalent exposure. Small premium does not imply small option risk.
9. Risk cannot be overridden by a strategy, an LLM, majority agent vote or a profitable backtest.
10. Correction, split or assignment is valid after an order is operationally closed; a “terminal” state does not freeze economic truth forever.

## 3. Instrument lifecycle rules

| Family | Required calculations and events | Qualification blockers |
|---|---|---|
| Spot/cash equities | Inventory cost basis, settlement, commissions, spread/FX, dividends, splits, mergers, symbol changes, delisting | Unknown corporate deliverable, stale adjustment factor, missing settlement rules |
| Margin/short stock | Borrow availability/rate, locate where needed, proceeds restrictions, financing, recall/buy-in, dividend obligations | Unverified borrow, recall, insufficient margin or account permission |
| Linear futures | Contract multiplier, tick value, expiry, variation margin, roll, cash/physical settlement | Missing multiplier/calendar, first-notice/delivery risk without an approved handling policy |
| Inverse futures/perpetuals | Payoff in settlement currency, collateral FX, funding schedule, mark/index differences, margin tiers and liquidation | Treating inverse payoff as linear, stale funding/margin metadata |
| Options | Strike, expiry, right, exercise style, adjusted deliverable, premium, Greeks, volatility surface, assignment/exercise, multi-leg interim risk | Assuming 100-share deliverable, missing exercise/assignment feed or exercise buying power |

Corporate actions use the provider's effective events and qualified point-in-time metadata. Split-adjusted price history must not cause a second economic split of live holdings. Option Greeks are model-dependent estimates with model/market timestamps; scenario stress remains necessary. Multi-leg orders reserve intermediate leg risk unless atomic package execution is actually guaranteed. Stop loss is a trigger mechanism, not a maximum-loss guarantee.

## 4. Exact reference vectors

These deliberately small fixtures are independent test oracles, not simulated performance results.

| Vector | Inputs | Expected economic result |
|---|---|---|
| Cash round trip | Start USD 1,000; buy 2 shares at 100, fee 1 USD; sell 1 at 110, fee 0.50; mark remaining share at 105. Cost basis excludes separately expensed fees. | Cash 908.50; position 1; gross realized P&L 10; gross unrealized 5; fees 1.50; equity 1,013.50; net P&L 13.50. |
| Partial + cancel | Buy 10 units; fills 3 and 2 with unique IDs; cancel remaining 5 confirmed | Position increases 5, never 10; reserve released only for cancelled remainder and unused fee allowance; repeated fill event changes nothing. |
| Linear future | Long 2 contracts, multiplier 10, price moves 100 → 103 | Gross mark P&L 60 quote-currency units, before fees/funding. If variation margin already paid, do not count it again as unrealized profit. |
| Inverse contract | Long 100 contracts each worth 1 USD, entry 10,000 USD/BTC, exit 11,000 | Gross P&L = 100 × (1/10,000 − 1/11,000) BTC = 0.00090909… BTC, rounded only at the explicit settlement boundary. Exact rational intermediate or a declared precision policy required. |
| Perpetual funding | Long linear notional 1,000 USD, payable funding rate 0.0001 | Funding debit 0.10 USD under this fixture's sign convention. Actual provider convention must map to it explicitly. |
| Split | 10 shares, unit basis 100, 2-for-1 split | 20 shares, unit basis 50, total basis 1,000, no P&L solely from split. Adjusted option deliverables handled separately. |
| Long option exercise | One standard call, strike 50, deliverable 100 shares, premium paid 200, exercise fee 0 | Exercise exchanges 5,000 USD for 100 shares and removes the option. Total economic basis 5,200 if the chosen reporting policy capitalizes premium; no free 100-share gain. |
| Short covered-call assignment | 100 covered shares and one short standard call at strike 50 | Deliver 100 shares, receive 5,000 USD, remove option obligation; include previously received premium and fees once in outcome attribution. |
| Deposit neutrality | Equity 1,000 → 1,500 solely by deposit 500 | Investment P&L 0. Return calculation uses external-flow timing; no 50% strategy return. |
| Bust/correction | Fill 2 at 100 later corrected to 2 at 101 | Reverse original posting set, post corrected set; net cash difference −2 before fee corrections; audit retains both observations. |

Test both accounting conventions and provider statement reconciliation. Tax reporting cost-basis rules are a separate jurisdiction-specific reporting feature; they must not silently redefine trading P&L or become an assumption about this user's residence.

## 5. Independent risk service

Risk receives the latest authoritative portfolio version, working/UNKNOWN orders, reservations, capability/instrument versions, valuation freshness, scenario distributions and intent. It returns an explicit per-rule decision. Run a final admission evaluation inside the writer transaction; a decision calculated before another strategy reserved funds is not sufficient.

Hard policy dimensions: maximum capital-at-risk, gross/net leverage, per-position/per-asset/per-venue concentrations, correlated factor exposure, liquidity/participation, spread/slippage, daily realized-plus-unrealized loss, peak-to-trough drawdown, collateral buffers, scenario tail loss, data/clock freshness, short borrow, settlement, option exercise obligation, futures delivery cutoff and allowed actions. Thresholds are user policy or pre-registered strategy envelope values, not arbitrary constants invented by the architecture.

Use covariance/factor concentration as estimates with uncertainty, supplemented by explicit stress: price gaps, vol spikes, correlations moving toward one, unavailable exits, funding/borrow spikes, spread widening, stablecoin/collateral depeg and venue loss. VaR alone is insufficient; show expected shortfall/tail scenarios and liquidation headroom. Probability-of-ruin estimates require a stated model and sensitivity analysis; do not label them certainties.

Risk reduction is evaluated on the actual portfolio. A sell can increase short risk; closing one hedge leg can increase aggregate risk. Define separate action permissions for reduce exposure, preserve/repair protection, cancel and flatten. A risk-reducing action may need different admission bounds, but cannot bypass account identity, duplicate protection or valid sizing.

## 6. Portfolio construction and economic objective

Strategies emit distributions and thesis candidates, not final order authority. The portfolio allocator compares feasible implementations across instruments/accounts with available capital, correlation, costs, horizon, liquidity and capacity. A gold thesis may map to a permitted fund, future or option; cross-provider allocation cannot spend funds outside the funded account. It can recommend an external transfer to the user, but the trading agent cannot perform withdrawals.

Use a constrained optimization objective combining expected net return, variance/tail-risk penalty and turnover/cost. Risk penalties are decision preferences, not fictitious expenses in cash reporting. Begin with transparent constrained allocation and cash/no-trade fallback; optional solvers require feasibility verification and deterministic fallback. Position sizing depends on uncertain edge, downside and liquidity; fractional Kelly-style analysis, if used, is bounded and stress-tested rather than treating estimated probabilities as truth.

Separate metrics: gross signal return; execution-adjusted trading P&L after spread/slippage/fees/financing/funding/borrow; operating return after attributable data/model/hosting costs; capital-efficiency and capacity; risk distributions. Spread/slippage already embedded in actual fill P&L must not be subtracted a second time; attribution decomposes a benchmark difference. Report turnover, drawdown, tail loss and uncertainty alongside return, not win rate alone.

For small capital, enforce minimum lot/notional, fee floors, diversification feasibility and subscription/compute budgets before proposing a trade. Example budgeting is a feasibility calculation, not a promised target: an operating cost of 10 currency units on capital 100 requires 10% of capital merely to cover that cost over the same period. Prefer idle capital and a deterministic low-cost path when additional complexity has no measured net value.

## 7. Reservations and multi-account coordination

Each admitted intent reserves worst-case required cash/collateral plus fees and plausible slippage under bounded execution terms. For unbounded market orders, policy must provide a defensible exposure/cost bound or reject. The reservation ledger is shared across strategies on an account. Portfolio-wide policy additionally coordinates venue/currency/factor exposure without pretending settlement or transfer is immediate.

A reservation is reduced by unique confirmed fills and evidenced terminal remainder cancellation; filled exposure transfers to the position/margin book. Unknown quantities remain conservative. Deposits, withdrawals and provider adjustments are external flows reconciled independently. Concurrent intents use serialized/optimistic versioned admission, with retry of the risk calculation rather than reuse of stale approvals.

## 8. Qualification evidence

Property tests vary order/event order, split/fee/FX precision, partial fills and corrections while preserving conservation. Differential checks compare LEAN, independent fixture formulas and provider statement examples. Stress tests cover margin tiers, exercise/assignment overnight, futures settlement and account mode changes. Each discrepancy has an explained classification; tolerance cannot silently mask a unit, sign or multiplier error. Release gates require all financial fixtures and crash/replay invariants to pass on the exact build and schema.
