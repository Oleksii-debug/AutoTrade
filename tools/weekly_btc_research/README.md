# Weekly BTC momentum on WhiteBIT

This study addresses the cost of frequent decisions. The spot account buys BTC with 35% of its available cash when the last Saturday closed above the preceding Saturday. It carries the same units while the signal stays positive. A non-positive signal returns the account to cash. Decisions are recorded on Sunday UTC and priced at Monday's daily open. The protocol file freezes the allocation, dates and cost assumptions. A daily opening price models a fill. It does not verify available size in the order book.

Run from the repository root with Python 3.11 or newer. Supply an output directory on a persistent local disk.

```powershell
python -m tools.weekly_btc_research.history --output D:\AutoTradeData\weekly-btc
python -m tools.weekly_btc_research.forward --output D:\AutoTradeData\weekly-btc
python -m pytest -q research/tests/test_weekly_btc_research.py
```

The historical command downloads public daily BTC_USDT candles once and authenticates the local archive on replay. The observed normalized dataset had 1,781 bars and SHA-256 `f245d751271997b913e64bc6220edfdb8016fa62a7dad38dc25ba6318948f092`. The output includes modeled costs, weekly returns, closed trades and peak-to-trough drawdown marked daily.

| Observation period | Weeks | Round trips | Net base | Net stressed | Passive base |
| --- | ---: | ---: | ---: | ---: | ---: |
| 10 Jan 2022 to 30 Dec 2024 | 155 | 43 | +4.757% | +2.418% | +43.183% |
| 30 Dec 2024 to 16 Nov 2026 | 98 | 25 | +1.582% | +0.257% | −10.657% |

The figures cover each entire period. On the later cut the median weekly return was zero. Three weeks gained at least 3% and none reached 5%. The base scenario had a 9.97% daily-marked drawdown. The stressed cumulative return's 95% four-week block bootstrap interval crossed zero. Comparing the two historical periods also shows how strongly the choice of benchmark and market period affects the conclusion.

Schedule the forward command daily while the computer is available. It records the first Sunday signal on 22 Nov 2026, the first Monday mark for 23 Nov, and the terminal mark for 24 May 2027. Marks use completed daily candles and become available on or after Tuesday UTC. Rerunning validates the stored market payloads and rebuilds balances. A missed Sunday invalidates the continuous portfolio. The command uses the public market endpoint and local arithmetic. No exchange credentials or model service is needed.
