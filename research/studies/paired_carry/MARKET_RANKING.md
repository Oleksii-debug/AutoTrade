# Weekly market selection on WhiteBIT

The local analyst trains on observed weekly prices and funding for ADA, BTC, DOGE, ETH, SOL and XRP. It compares six markets on relative price movement, volatility and trading volume. Two models trained on different 24-week windows must agree on an expected spread above modeled costs. A Sunday forecast is recorded before Monday's market clock. Once the week has closed, the review measures the selected pair against cash and all collateralized alternatives using the same 1,000 virtual USDT. The review keeps later public observations separate from the recorded forecast. This research job has no exchange execution or language-model service.

The momentum experiment draws on the cross-sectional hypothesis in [Common Risk Factors in Cryptocurrency](https://www.nber.org/papers/w25882). The WhiteBIT experiments use six liquid markets, exchange lot sizes, an hourly collateral check, actual funding settlements, modeled slippage and fees. They test a different market universe and period from the paper.

| Six-month block beginning | Buying the prior loser and shorting the winner, fixed Monday entry | Same pair after hourly recovery confirmation |
| --- | ---: | ---: |
| 20 Nov 2023 | −14.92 | −51.52 |
| 20 May 2024 | −26.58 | +170.49 |
| 18 Nov 2024 | +20.48 | +95.25 |
| 19 May 2025 | −92.79 | −34.79 |
| 17 Nov 2025 | +54.70 | −12.61 |
| 18 May 2026 | +47.92 | +27.48 |

Each block began with a separate 1,000 virtual USDT. Entries assigned up to 25% to each leg. The two 2023–24 blocks were obtained after freezing the recovery rule. Neither qualified. In the two recent blocks, the learned six-market selector returned −19.36 and −38.81 USDT after costs. Across those 52 weeks, inspecting all 30 possible pairs after each week ended found 24 weeks with at least one modeled gain of 20 USDT on a fresh 1,000 USDT account. That count measures opportunities within this particular sizing and holding period. Future prices were used only for that diagnostic.

The 48-hour confirmation variant made at most three entries a week using the same ranking. Its six consecutive half-year results after costs were +55.88, −16.51, −14.66, −144.64, +13.43 and +9.79 virtual USDT per 1,000. In the weakest period the confirmed positions lost 96.91 USDT before modeled execution costs. Reducing fees alone could not make that period profitable. Run `python reversal_two_day.py` after the weekly confirmation replays to reproduce the paired controls, costs and individual weeks.

Run the following from the study directory with the existing AutoTrade checkout and market archives.

1. Run `momentum_pair.py` and then `momentum_pair.py --earlier-year`. The optional `--with-exits` tests closing a position one hour after the first observed 2% gain or loss on the whole account.
2. Run `pair_selection.py` for the conditional weekly reversal model. Run `reversal_confirmation.py recent`, `prior` and `additional` for the hourly confirmation experiments.
3. Run `market_ranking.py` to build initial experience for 104 weeks and the chronological research replay. Run `market_ranking_forward.py --historical-opportunity` for the all-pair comparison.
4. Run `install_market_ranking.ps1` to register the research worker. It runs on Sunday and Monday at 06:20 local time and on login. Each call adds at most two missing historical labels. The 26-week forward period starts on 23 November 2026 at 01:00 UTC. A missing forecast stays missing. The subsequent price label still contributes experience. The fitted models, forecasts and reviews persist in `market-ranking/learning.sqlite3`.
5. Run `replay_market_ranking.py` to exercise forecast, later market review and restart on archived time in a separate database. Run `python -B -m unittest test_market_ranking test_six_hour test_carry test_direction_watch` for regression coverage.
