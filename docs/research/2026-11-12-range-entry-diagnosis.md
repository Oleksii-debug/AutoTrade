# Range entry diagnosis — 2026-11-12

The range-deviation simulator now cancels a setup if its precommitted stop is crossed while waiting for confirmation. A later favorable close cannot revive an invalidated thesis. Long and short regressions exercise a candle crossing both the confirmation and invalidation levels. Detection and confirmation rejection counts are retained in each result.

The unchanged 60-minute configuration on 5,184 previously available M15 TRAIN candles per SOL, XRP and DOGE detected 34 setups: 12 invalidated, 14 confirmed before the frozen minimum age, 5 expired, and 3 reached entry evaluation but failed reward/risk. No trade was admitted. These are first-rejection counts, not independent feature effects.

A separate first-bar-confirmation diagnostic retained the original risk and after-cost gates and admitted zero trades. Its 17 entry evaluations had gross reward/risk between 0.23 and 1.68, below the precommitted 2.0 minimum even before costs. Earlier confirmation alone does not fix this entry geometry. DEVELOPMENT and FINAL were not consumed. No parameter was promoted.

Next hypothesis: confirmation followed by a causally observed retest might improve entry geometry. This requires modeling unfilled limits, expiry, gaps and stop/target ordering; a hypothetical better price is not a filled trade or profit. No retest strategy is enabled by this change.

Local receipt hashes in runtime/course-range-causal-diagnosis-20261112:
- baseline: c6cae7fc6cbedd3680e3811e18cda14cd8797abe3ca4791682ef2cca8bd1536f
- first confirmation: 61229630ae9fd1f1559c5a6f8e32cde7881fe5e0747228639843c8cd885b05ac
- entry geometry: d8a94f59126703ede98e459e5e2efe6bd560c06588ce5458de9c85b0abd07d69

Validation: 11 focused tests and 6 subtests passed locally. This is simulated research; no real orders, paid model calls, or earned money.
