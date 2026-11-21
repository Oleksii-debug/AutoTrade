# Short exposure with a protective stop

The older untested block from November 2023 to May 2024 changed the assessment of the permanent short. It lost 536.81 virtual USDT per 1,000 as prices rose. The defensive rule entered a downside channel break, sized the position by its planned stop distance, and bought it back if the stop was reached. It lost 12.31 USDT in that same period.

Across six consecutive 26-week simulations, each funded independently at 1,000 USDT, the protective strategy returned −12.31, −0.98, +0.48, −26.22, +13.95, and +45.74 USDT after costs. Its largest marked drawdown within any period was 3.52%. It reached the 2% weekly target twice in 156 weeks. Stop-based sizing reduced losses in adverse conditions and also reduced gains in favorable conditions. The strategy did not qualify for deployment.

Run `python protected_short.py` for the complete experiment, including the permanent short comparator, quoted trading costs, settlement history, adverse opening gaps, and conservative funding for stops inside a daily candle. The eligible decision requires both the early and latest registered dates to pass.
