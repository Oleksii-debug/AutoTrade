# Owner target: 1% net account equity per hour

On 2026-11-12 the owner raised the benchmark from 0.51% to 1% per complete UTC clock hour. The metric uses account liquidation equity after modeled costs, including flat hours. The existing linear four/eight-hour comparison targets are now 4% and 8%; these are targets, not forecasts. Archived receipts retain their original targets. Strategy, allocation and authority are unchanged.

A diagnostic considered one 30–60-minute trade in each complete hour, at M15 price endpoints, with perfect hindsight over direction, time and SOL/XRP/DOGE. It includes the declared 15bps execution-cost envelope and estimated carry. It ignores stop constraints, has no causal predictor, and is not a strategy or universal price-path bound.

Across 2,159 common recent historical hours:
- At 25% account notional, 16 oracle hours reached 1%; average best return was 0.164210%.
- At hypothetical 100% notional, 377 reached 1%; average best return was 0.656842%. This sizing was not enabled.
- Selecting the best market with future knowledge overstates what an executable policy can capture. One trade every hour is also more permissive than the current one-entry-per-day policy.
- Receipt: 7cd9661c850dde49c030659bc0e42500910d015f14964fd170b9d92cf1820982 in runtime/hourly-one-percent-20261112.

The 0.055% taker fee assumption matches WhiteBIT's published Level 0 table checked on this date: https://help.whitebit.com/hc/en-gb/articles/14556440496157-WhiteBIT-VIP-program . Account-specific rates, spread, slippage and funding remain distinct inputs.

Conclusion: the current constrained strategy does not establish the requested hourly outcome. No profit, increased exposure, new model training, or real orders are claimed by this benchmark change.
