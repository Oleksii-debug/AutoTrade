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

## Completed retest experiment

An opt-in research configuration now waits for confirmation, then a later completed candle that touches the original range boundary and closes back inside. Execution remains at the following open with a renewed economic gate; it is not an assumed limit fill. Pending invalidation and a four-bar retest expiry apply. The default remains disabled.

The single specified experiment used minimum confirmation age 1 and a 60-minute holding cap, retaining the course risk budget (1% equity), maximum notional (2x simulated equity), and 20/5/5 cycle gates. This differs from the Bollinger simulator's 25% notional and is not a matched return comparison. On the same three TRAIN partitions it admitted one DOGE trade: -5.4057686 per 1,000 simulated quote units, exited at the holding cap. SOL and XRP admitted zero trades. DEVELOPMENT and FINAL remained locked. This is a rejected candidate, not a promoted strategy. No follow-up parameter search was performed.

Receipt: 5acc93cc1992a727108a809f60a708d0d9630c7b8451c55c81dd046dd6796815, with protocol saved before execution in runtime/course-range-retest-20261112. Reproduce using authenticated last-8,640-bar SOL/XRP/DOGE cuts, screen_universe(train_count=5184, development_count=1728), and RangeDeviationConfiguration(maximum_hold_bars=4, minimum_shift_confirmation_bars=1, require_boundary_retest=True).

Validation: 12 focused tests and 8 subtests passed, including long/short confirmation -> later retest -> next-open execution.
