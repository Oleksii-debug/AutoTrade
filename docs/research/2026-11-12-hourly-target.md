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

## Portfolio evaluation on a later quote

[PR #2553](https://github.com/Oleksii-debug/AutoTrade/pull/2553) reuses the running market collector for a shared virtual account. [PR #2554](https://github.com/Oleksii-debug/AutoTrade/pull/2554) freezes the features at each half-hour decision and trains on the next 15-minute boundary's distinct quote and the quote 30 minutes after entry. Reported one-hour windows start at minute 15 and contain two matched holds from the same policy version. At 19:15 Europe/Budapest the local observer matched a previously recorded HOLD to a distinct WhiteBIT quote and restored it across a process restart.

The retrospective SOL replay used identical dates, costs, 25% simulated notional and delayed fills in all three runs. Historical quotes were modeled from M15 opens and declared spread. The final 30 of 60 previously used days yielded 719 matched full-hour windows per run.

| Training labels | Closed directional intervals | Compounded after costs | Hours at or above 1% |
| --- | ---: | ---: | ---: |
| Decision quote, prior fit | 149 | −6.1025% | 0 |
| Later quote, 336 retained half-hour outcomes | 0 | 0.0000% | 0 |
| Later quote, 48 retained half-hour outcomes | 203 | −7.1199% | 0 |

The 48-outcome comparison matches the prior fit's eligible half-hour lookback on a continuous timeline. The longer fit held throughout the replay. Neither establishes a positive edge. Forward decisions and historical replay remain in separate state. The 24 focused Windows tests for the exact merged code covered label timing, future exit rejection, restart and policy-version separation.

Evidence SHA-256 values are `bc3d5c9f6d7fa03374cfe1b2297d9b15da8fb632d0cdb47200a3467a894d870e` for the default fit and `d72288c2c28484624811486cafe35f1623144d514ede8eef63a66e7a95a064c3` for the 48-outcome comparison.

## Additional futures and execution-cost attribution

The same retrospective headroom calculation on 14 authenticated M15 markets yielded 126 of 2,159 complete hours reaching 1% with perfect hindsight, up from 44 on the original eight markets. NEAR appeared in 57 of the newly qualifying hours and BCH in 17, with some overlap. The portfolio used 25% notional and a 26 bps round-trip envelope. TON had no verified M15 archive for this cut. The diagnostic does not measure executable strategy returns. Receipt `f2c77b86f53a24b3ea45e0791e57cbc08cea673bf6d1555361570d5747ef148d`.

On a separate, earlier 60-day cut, the unchanged delayed-entry region learner abstained throughout the scored 30 days across the six added markets. It produced 719 complete hours at 0%. Receipt `936a64f209f8748fed932ce5f2e8763b75bcc12d3b9f46d32b27de2d7103ac6b`.

One separate six-market XGBoost model trained on 34,260 earlier rows and was scored on the following 30 days. Features used completed M15 bars and cross-market relative returns. One virtual account selected a side or held every half hour. Entry was 15 minutes after the decision and exit followed after another 30 minutes. Over 718 full DEVELOPMENT hours the best was +1.08594%, with one target hit. Compounded return was −0.33458% and maximum drawdown at hourly marks was 2.33682%. The next 30 days were reserved and the model was not promoted.

The postmortem reproduced the DEVELOPMENT result from saved weights. The 44 directional intervals produced +1.33646% summed midpoint movement and 1.65394% modeled execution costs, leaving −0.31748% summed net. At the tested allocation, approximately 2.89 bps less cost per trade would have reached summed breakeven in this sample. No saving or fill quality was verified. DEVELOPMENT receipt `dbba4b64ba966029c56922401c2d3fbef8f26edfdaa6ec7f87a8f3f31e424cde`, review `cb08fa2574122f6dc23b0dec6e31b00670eb31af95ad342a15ad22933440fb5a`, model `f73572b68389537e7ebbfb5a6ead4ce4020cb13bba210d472cd9714e180f5e0b`.
