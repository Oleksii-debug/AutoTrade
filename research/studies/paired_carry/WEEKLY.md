# Weekly local learner

Run `python swing.py` and `python weekly_agent.py` with NumPy installed. The learner saves the 104 chronological fits in `weekly-agent-results.json`. Each fit uses 234 past market-week observations. Targets end at least one week before the decision. The first 52-week portfolio gained 44.18 virtual USDT on 1,000 in one ETH week. The second stayed in cash all year. The equal-weight alternative on the same active week gained 38.96 USDT.

`python weekly_agent.py --transfer` repeats the frozen rule on LTC, LINK and ADA. That model lost 23.80 USDT and then stayed in cash in the second year. These tests did not establish repeatable selection skill. The model has no trading authority.

`python weekly_forward.py` maintains an independent prospective forecast ledger. Its first decision window is Sunday, November 22, 2026 UTC for the week opening Monday, November 23. Windows invokes it daily and at login. It records decisions only within their original windows, then settles their forecast errors and hypothetical returns on later public data. If the machine misses Sunday, the forecast remains missing. Stored coefficients, public quotes and results persist in `weekly-forward/learning.sqlite3` and `weekly-forward/raw`.
