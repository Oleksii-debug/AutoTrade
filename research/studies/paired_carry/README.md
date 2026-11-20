# WhiteBIT paired-position experiment

Run with Python 3.11+ from an AutoTrade checkout. Set `AUTOTRADE_CARRY_STATE_DIR` to the persistent data directory. When running a separate copy of the scripts, set `AUTOTRADE_CHECKOUT` to the source checkout.

1. Run `python study.py` to cache and verify public hourly candles and realized funding settlements, then replay two separately funded 26-week periods. Retain the entire data directory to reproduce the input snapshot.
2. Run `python pairs.py` for the separate registered BTC/ETH relative-value rule and `python live-screen.py` for a point-in-time market comparison.
3. Run `python forward.py` hourly for the qualified fixed BTC hedge. The simulator reads public order books, records paired virtual executions and funding, and writes the cost of liquidating the position to `forward/Рахунок BTC.txt`. It reads no account credentials. Each hour has one durable SQLite ledger entry. The next successful invocation reconciles missed funding. Intrahour margin changes and temporary price excursions between observations are not captured in the hourly forward trial.
4. Run `python test_carry.py` to check execution, cash accounting, chronological inputs and restart handling.

The fixed BTC comparator returned +1.80 and +9.09 USDT per 1,000 USDT under stressed costs in the two historical periods. Zero of 52 weeks reached 2%. The four-week selector stayed in cash in the first period and gained 3.20 USDT in the second. The separate BTC/ETH mean-reversion policy lost 90.88 and 77.46 USDT across its two 24-week periods. Only the fixed BTC comparator entered prospective simulation. All P&L is simulated and all periods include time in cash.
