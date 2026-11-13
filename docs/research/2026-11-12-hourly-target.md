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

## Prospective cost observations on 13 November

The first public NEAR, SUI and BCH snapshots showed spread-plus-standard-taker-fee estimates of 22.70, 23.45 and 25.73 bps for an immediate round trip, before additional slippage and carry. These are point observations with capacity checked separately. The historical 15 bps envelope was lower than all three. The prior development sample would have needed about 2.89 bps in savings just to break even. [WhiteBIT futures fee schedule](https://help.whitebit.com/hc/en-gb/articles/14556440496157-WhiteBIT-VIP-program)

An earlier-TRAIN 90th-percentile confidence threshold was evaluated once on the previously used DEVELOPMENT slice with identical XGBoost weights. It accepted one trade with a modeled +0.03296%, below the predeclared 12-trade minimum. The reserved period remained unopened. Receipt `8578d2e7d9d34ba8beaa212621a5548535dd42ecd0aec88709b06d568aa1acdf`.

The fixed 00:30–01:00 Budapest virtual probe found one side, SUI SELL, with both a fresh passive book and later public trades beyond its limit. The recorded exit book displayed sufficient size. Conditional on a full hypothetical entry, maker entry and taker exit lost 0.46791 quote units on 250 notional. The taker-reference version lost 0.94705 on the same modeled quantity and exit, a difference of 19.17 bps. No fill or realized P&L was verified. The other three sides lacked sufficient entry evidence. Wave SHA-256 `e39a40eae71f8a730b0ac35948ef7a23e9f861e27dfdfe7256fe763110d36a4b`.

A second wave lacked a fresh book just before eligibility. The separately pre-recorded 01:00 exact-book wave had fresh passive books on all four sides but recorded no eligible trade volume beyond any limit over 120 seconds. The available stream evidence leaves all four hypothetical fills unproven. Wave SHA-256 `1a00c70f11ea4f88a7cb274e680442dac0cf34c24ae55aa82f08a028463b59f8`. No strategy promotion follows from these results. [WhiteBIT matching rules](https://docs.whitebit.com/concepts/matching-engine)

## Prior-day sweep and funding controls

A frozen prior-UTC-day sweep rule used one account over BTC and ETH, 25% allocation, a 15-minute delayed entry, a maximum 45-minute hold, and at least 2:1 estimated reward to risk after costs. Across 437 TRAIN days there were 1,041 closed positions, with −32.99% compounded net return and 33.57% maximum realized-equity drawdown. The run stopped at TRAIN, leaving DEVELOPMENT and FINAL unused. Historical execution uses authenticated M15 opens, a 15 bps modeled round trip, and a daily carry reserve. The result receipt SHA-256 is `b8e84d5337fbd3e89f540146b777b662dcde7d8e137c0b871e17555a8eaca63f`.

A separate 60-minute price-response diagnostic entered 15 minutes after a public funding settlement using its observed sign. TRAIN was negative on both markets. In DEVELOPMENT, the net mean was +2.32 bps on BTC and +5.89 bps on ETH after modeled fees and spread. All 16 decisions per market were SHORT and shared the same 16 timestamps. The matched same-clock always-SHORT control returned the exact same result, so the measured incremental effect of using the funding sign was zero. No combined account return was computed. Slippage and historical BBO were unavailable. The result and control hashes are `570eb7734330fa390a6d384f0b5fed1dbead86608b41b4f2694a7eba9144af91` and `616458d9b361c3a9b881796c591c10baff6ed99391dc85ab0e47302c36d82c32`. [WhiteBIT funding history fields](https://docs.whitebit.com/api-reference/market-data/funding-history)

The separately initiated BTC/ETH live-quote observer was imported into the existing Windows worker's checkpoint without resetting its initial training labels or start time. It recorded its first managed decision at 01:30 Europe/Budapest. The policy held and no independent interval had yet settled. [Managed observer change](https://github.com/Oleksii-debug/AutoTrade/pull/2565)

## UTC-session VWAP reclaim and continued collection

A frozen BTC/ETH rule entered at the next M15 open after a completed candle crossed back over cumulative UTC-session VWAP. It used a single account, 25% exposure, a 45-minute cap, fixed 40 bps stop and 80 bps target, a 15 bps modeled round trip and a carry reserve. Ambiguous intrabar stops and targets were resolved stop-first. Zero-volume starts had no defined VWAP. The sole evaluation used 437 TRAIN days with authenticated WhiteBIT candles.

The rule recorded 4,537 trades with 1,303 wins. Gross summed equity return was −3.32034%, compounded after-cost return was −82.44723%, and maximum drawdown was 82.47597%. The rule failed the TRAIN gate. DEVELOPMENT and RESERVED remained unopened. Protocol SHA-256 is `bbe389f30734ba0580f70894e6a9e641345dfdfd313d41b34b0dd8a4c4a4f806` and result SHA-256 is `93f891f70ab5a2936eb53d3d997228922b20c2c985820a670f22c614f88355fe`. Execution costs are modeled. These are virtual results.

At 01:45:24 Europe/Budapest the BTC/ETH account matched its 01:30 HOLD to a distinct, later quote. The three-market account then had nine settled intervals, two complete flat hours and zero target hits. An earlier outage left three missing 15-minute boundaries, with no depth snapshots from those times. The two unmatched intervals stay excluded. The watchdog now launches directly as a console-less Windows scheduled task and restores both checkpointed observers. [Windows worker isolation](https://github.com/Oleksii-debug/AutoTrade/pull/2568)

The same futures quote response now supplies timestamped open interest, mark price and index price for BTC, ETH and SOL in the decision journal. This adds prospective evidence for studying whether open interest and basis improve a price-only model. Aggregate open interest alone does not identify liquidations. [WhiteBIT futures fields](https://docs.whitebit.com/api-reference/market-data/available-futures-markets-list), [decision journal change](https://github.com/Oleksii-debug/AutoTrade/pull/2569)

## Fixed course rules and scheduled FOMC statements

One offline assessment reused the exact managed Quasimodo and Range Deviation configurations on the first 45,000 authenticated M15 candles for each of BTC, ETH and SOL. Quasimodo produced zero signals. Range Deviation produced 313 setups and 18 confirmed signals, of which 17 failed the after-cost reward-to-risk gate. One SOL trade closed for +5.71236 simulated quote units on its own 1,000-unit account. Neither family reached the three-trade TRAIN minimum. DEVELOPMENT and RESERVED stayed closed. Result SHA-256 `1cb66513781d6efc6945f16d538206e096455659e5e43c3fb31870c345cd6e05`. [Persisted signal and rejection counters](https://github.com/Oleksii-debug/AutoTrade/pull/2573)

That SOL trade used the course's 24-hour maximum hold. A single follow-up on the same TRAIN data kept all rules fixed and shortened the cap to 60 minutes. The same trade closed after exactly one hour for +0.15325 on 1,000 simulated quote units (+0.015325%). BTC and ETH still opened no positions. The rule failed TRAIN on sample size and left later partitions closed. Result SHA-256 `e5f760399a1498a24c6d85ef79fcf2df5d82dc5480da98d73f732e1926be80ba`.

For BTC and ETH on scheduled FOMC statement dates, the corrected rule used the completed 14:00–14:15 Eastern candle to select direction, entered at 14:30 and exited at 15:00. One account allocated 25% total across the two markets. Modeled execution cost was 15 bps per trade. Controls used the same local clock seven days earlier. An initial diagnostic used a 15-minute exposure from 14:15 to 14:30 and returned −0.44219% on the 2025 dates. Those eight dates were reused for the corrected TRAIN, which returned +0.33020% versus −0.21125% for its controls. [Official FOMC schedule and release time](https://www.federalreserve.gov/newsevents/pressreleases/monetary20240809a.htm)

The unchanged corrected rule returned −0.57208% on all seven completed 2026 scheduled dates through 13 November, versus −0.66295% for the matched controls. The event account beat control in three of seven pairs and the median paired difference was −11.06 account basis points. The candidate failed DEVELOPMENT and FINAL stayed closed. TRAIN and DEVELOPMENT result SHA-256 values are `8d4d5a4cb5f35e47bb1398c0afdfe09a041f75d49db2e78295c8ba5bf8773eb4` and `dee6303f09969408c643cdfb3251955b18cf532a522e85300b4fa41356f57a0e`. [Federal Reserve calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)

## 13 November morning attribution and live comparison

At 09:15 Europe/Budapest, BTC/ETH had seven complete current-policy hours with five flat, one positive and one negative. Its best hour was +0.076167%, its worst −0.257684%, and its compounded after-cost return −0.181714%. The three-market account observed ten of thirteen eligible hours and compounded to −0.32876%. Neither account recorded a +1% hour.

The two BTC shorts each held for 30 minutes and returned +0.1086875% and −0.2249455% before modeled costs. Nearby displayed depth covered the modeled 250 quote units for both legs of both positions. Removing the same single training outcome reversed both estimated after-cost edges. An independent Windows task now compares the unchanged policy with a prospective research-only guard that rejects a directional selection if removing any one comparable settled training outcome changes its net estimate to zero or below. Both arms share the underlying quote journal, later entry and exit, modeled costs and selected market. The comparison begins at 10:00 Europe/Budapest and runs for up to 30 days, with no automatic promotion. Protocol SHA-256 `1f7eac1451f8c8affcb44fce83e638a46bd170bf22da12a7918316c698fe3c95`.

The matched live quote archive also permits a hindsight check over the same fixed endpoints. In each 30-minute slot, the diagnostic chose the best after-cost label among HOLD and the available directions and markets. It required every listed market to have both complete, aligned slots. Even this retrospective chooser reached +1% in zero of seven complete BTC/ETH hours and zero of ten complete BTC/ETH/SOL hours. The best was +0.525818%. The result applies to the fixed windows and observed quotes. Receipt SHA-256 `829aa7cb321d57db0e75e13ee5794f37c83f3bbbb138b7853453f47b617848ac`.

A fixed BTC benchmark-reversal rule used the completed New York benchmark window, a 30-minute hold, and an earlier same-day control. On 247 authenticated 2025 TRAIN observations, the two accounts returned −7.67480% and −7.39797% after modeled costs. The rule failed TRAIN. Result SHA-256 `a4c3f7881d11357217f44f1ad6706e0c186f856da2d2f9fe2c1dd3e6112ae5be`. [CME CF benchmark methodology](https://docs.cfbenchmarks.com/CME%20CF%20Reference%20Rates%20Methodology.pdf)

A separate 2025 TRAIN study tested 30-minute BTC reversion after high-turnover shocks with prior same-clock and similar-volatility controls. At 25% allocation, 83 of 745 non-overlapping signals found eligible controls. After modeled costs, treatment compounded to −1.62822% and control to −3.11558%. The mean paired difference changed from +5.972 account basis points in the first half to −5.111 in the second. The rule failed the net return, stability, and minimum sample criteria. No later partition was used. Result SHA-256 `4d90488dcaa3f9079bd1c3fc421da32d348a3dfb87c456601ffe45070072acf7`. [Inventory-pressure research](https://www.nber.org/papers/w4193)

The existing WhiteBIT tapes keep exact signed NEAR/SUI trades and best bid/ask sizes for 24 hours. The coverage audit found one strictly usable 15-minute outcome per market and zero 30- or 60-minute outcomes in the initial cut. A read-only Windows task preserves closed 15-minute shards by local receipt time so that delayed prints and exact quotes survive source retention. It verified 35 bootstrap shards with 47,827 rows. Those records are coverage evidence. Prospective capture begins with the 09:00 UTC segment. Archive SHA-256 for the bootstrap is `445bf8f60bd67448e0f0535fd622b424f303a2e231a8950fb16bd5f430e69869`.

The depth collector candidate now measures synchronous storage work and wake-up delays, and reconnects after 15 seconds without a validated book update. Its isolated Windows smoke collected 112 observation buckets across five markets and stopped cleanly. [WhiteBIT order book stream](https://docs.whitebit.com/websocket/market-streams/depth) · [Recovery change](https://github.com/Oleksii-debug/AutoTrade/pull/2578)

## Direction and cost checks on 13 November

On exact matched live quotes, the two BTC/ETH directional decisions summed to −0.116258% of account equity before modeled fees, spread, slippage and carry. The two three-market portfolio decisions summed to −0.232143% before costs. One BTC trade appears in both portfolios. The corresponding compounded returns after costs were −0.181714% and −0.328760%. The opposite-side results on those same entries are retrospective controls and this sample is too small to qualify a direction change. Receipt SHA-256 `e559c3c5630b9c2e2bbe50689b08a020a4019107551965442f408df08692d872`.

On the 44 existing six-market XGBoost DEVELOPMENT selections, the original model direction compounded −0.33457%. A direction based on the previous completed 60-minute price change compounded −2.10375%, using the same entries, exits and costs. The 21 SUI selections returned +1.66288% versus −0.14760% for control. The 20 NEAR selections returned −2.15336% versus −1.54823%. The comparison isolates direction conditional on the original entries. FINAL remained closed. Receipt SHA-256 `d384978aafab48c25a147b91484322769f8db731befe5f242cc0738b3517ee5c`.

Replaying those same SUI selections with the recent public book's median 13.72 bps spread in place of the original 2 bps reduced their compounded return to +1.03860% over the full sample. Only four forecasts remained positive under that cost assumption. These spreads are a sensitivity input. The prospective Windows task preserves the original model weights and six-market ranking, then observes only SUI selections. A separate arm uses a live spread check recorded before the common modeled entry. The comparison holds for 30 minutes, records incomplete fills as unknown, and uses no paid model calls. Spread sensitivity SHA-256 `86af08422544c55f9f1847915a1af6ab6a0ef035dffe20eed599ce5c5c4b66b8`. Prospective protocol SHA-256 `ffdf6e688502689a5dd2e170b75469633d3a469150845101dc64e61155c7a809`.

The SUI selections occurred on four active dates. Under the median-spread stress, removing the largest observed winner leaves −0.04201%. The paired day-block resampling range spans negative and positive values. This sensitivity uses the same DEVELOPMENT sample. At the historical signal rate and the observed availability of book quotes, the two-week prospective program may finish with too few matched trades to evaluate the strategy. Receipt SHA-256 `2b00e586c7fe45484b9534d9b7ff31278b2e9fbae47cae5864170475f82b3232`.

A separate frozen 2025 TRAIN rule traded against a 1% hourly ETH dislocation relative to BTC, using opposite beta-weighted positions, 25% total modeled exposure and a 30-minute hold. Its 281 decisions compounded −14.75729% after modeled costs, versus −12.70083% for the continuation control. No decision reached +1% of account equity. DEVELOPMENT and FINAL remained closed. Result SHA-256 `d411436c63306ddf84f2e02c788756bf0404d4aeda8f4517fd3c21c1d78dc7fb`. [Relative-value research rationale](https://www.nber.org/papers/w7032)

## Prospective execution evidence on 13 November

The fixed 13:00–13:30 Budapest experiment compared immediate and passive entry using one BTC BUY and one ETH SELL. The virtual accounts were independent, with 250 quote units of notional per 1,000-unit account. Both immediate entries had sufficient displayed size. After modeled fees, spread, slippage and carry, the BTC reference returned −0.047169% and the ETH reference returned −0.025029%. BTC had zero qualifying trade-through volume. ETH had 5.34 base units against 13.197419 required to clear the displayed queue and the probe size. Both passive policies remained in cash. The result reproduced byte for byte from four closed archive shards, SHA-256 `d33d3a9c3944eff68bcd2747685a6aeb1c11b481c7b398b3124f372f5c72b893`. No orders or fills occurred.

A separately registered comparison tests whether persistent depletion of displayed touch size improves a signed-aggressor-flow rule. It uses 30 non-overlapping 30-minute BTC/ETH windows and a flow-only control. Both markets must pass continuity and coverage checks, and the same modeled base quantity must fit at entry and exit. The first comparison found no qualifying signal from the displayed-size rule, while the ETH LONG flow-only arm returned −0.114103% of its virtual account after costs. Its registered parameters and decision clocks were unchanged when the runner's archive-grace lookup was repaired. The remaining windows and the aggregate gate are pending. Protocol SHA-256 `8dddc73b9b0ac4ccaa5b6e693eaf08edc329fc1a431699bb34658272dd0d7ac5`. First receipt SHA-256 `4263e36101394b621f50e1eef4b7e9aac45dd5371b9dba250ddd64dbc79a5b11`.

At 15:00 Budapest, the BTC/ETH shadow account retained 12 complete hours, −0.181714% compounded and zero +1% hours. The stricter size-qualified projection covered ten all-HOLD hours at 0%. Two older directional hours remain in the original modeled ledger and lack the new recorded sizing basis. No strategy was promoted. [Size-qualified report](https://github.com/Oleksii-debug/AutoTrade/pull/2585)

Windows rolled back a full-volume copy when SOL's authenticated artifact store rejected changed NTFS identities. The second cutover retained that SOL store at its original location and moved the other five worker states and watchdog to D. Both database integrity checks passed. Six workers resumed, the exact prior journal prefix was preserved, and the 15:00 decision was published once on the new root. [Interrupted-decision recovery](https://github.com/Oleksii-debug/AutoTrade/pull/2584) · [Retained SOL store support](https://github.com/Oleksii-debug/AutoTrade/pull/2586)

One additional retrospective diagnostic on the same 14-market, 2,159-hour sample retained the 30-minute minimum, 25% allocation and 26 bps modeled cost. It allowed one trade to exit at the favorable high or low of an eligible M15 candle known afterward. The number of +1% hours increased from 126 to 170, and the average best hourly outcome from +0.420758% to +0.479368%. The maximum stayed at +4.139108%. This diagnostic assumes prior knowledge of the best market, direction and exit price. Intrabar order and executable size are unknown. No new holdout was opened. Result SHA-256 `cf020f6c1a2b5786a27d6452d5c330dd026dc5bdcd618030aa926b566ed7a7a1`.

## Matched fixed exits and live observations

An offline comparison kept all 44 six-market XGBoost DEVELOPMENT entries and sides fixed. It reproduced every 30-minute modeled outcome within `1e-15` and compared fixed exits at 45 and 60 minutes. Allocation was 25% with the original modeled spread, slippage, fees and duration-proportional carry. No model was refit and FINAL remained closed.

| Holding time | Sum of isolated after-cost returns | Median paired change from 30 minutes | Improved entries |
| --- | ---: | ---: | ---: |
| 30 minutes | −0.31748% | — | — |
| 45 minutes | +1.59575% | +1.60 account bps | 24 of 44 |
| 60 minutes | +1.17188% | −2.42 account bps | 18 of 44 |

Both longer horizons produced nine overlapping pairs involving 15 positions. Their returns describe separate positions under identical initial allocation. No combined account or hourly return is calculated. The result receipt is `fa16f9c6bd90577d2f9a74ed6c55617f780979791384542fce5d06df68eea952`. [Reproduction bundle](https://drive.google.com/file/d/1FsCIjnVMA2FC7-G8C9dYiwM7Kt1k7CRF/view)

SUI contributed much of the improvement. Applying the observed median spread as a sensitivity input to its 21 historical selections on four dates gave +3.05411% summed at 45 minutes and +1.04618% at 30 minutes. Removing the date with the largest improvement left +0.28779% summed across the other dates. The prospective comparator reuses only the original frozen SUI decisions and public WhiteBIT book, requires identical size at both exits, and waits for 20 chronological valid pairs. Its first scheduled Windows run completed at 17:30 Budapest with zero eligible pairs. [Spread sensitivity](https://drive.google.com/file/d/1zXSln4FHXTa8eYjOKcsKo5RuKZgUg8Eu/view) · [Prospective registration](https://drive.google.com/file/d/1YYv131Y_DerTaDSkUU-hmMcZA81C-YMT/view)

At 17:15 Budapest the BTC/ETH virtual observer had 15 complete modeled hours. It compounded −0.495663% after costs, with zero hours at +1% and a best hour of +0.076167%. The size-qualified projection covered 12 all-HOLD hours at 0%. The observer retained 31 settlements and 37 delayed training labels per market. The separate displayed-liquidity comparison had four terminal windows at 17:40, with one usable pair and three coverage or connection-continuity failures. No promotion or real orders occurred.

The historical course worker now commits closed training episodes and pauses before analyzing realized costs, exit reasons and pre-entry vetoes. It restores the saved phase after interruption and starts its next wait interval after analysis. Replay on 60,000 authenticated candles reproduced both original family results, with neither family qualifying at TRAIN. [Restart-safe review](https://github.com/Oleksii-debug/AutoTrade/pull/2590) · [Verified replay](https://drive.google.com/file/d/1x6VsBiD5yvVxeXW1Y-Gv7SQQDIsPOWk0/view)
