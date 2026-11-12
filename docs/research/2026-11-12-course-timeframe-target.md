# Course timeframe and exit translation — 2026-11-12

The owner's private consolidation lesson describes both partial profit at equilibrium and holding to the far range boundary. The simulator previously supported only full exit at equilibrium. The new explicit OPPOSITE_BOUNDARY research option fixes the target from the already observed range; default EQUILIBRIUM remains unchanged. Neither route guarantees profitable trading. Source wording is not published.

One frozen M5 experiment used BTC_PERP, ETH_PERP, SOL_PERP, 17,280 authenticated candles each (60 days ending 2026-11-12 00:00 UTC), a 96-bar range (8 hours), the existing three-bar confirmation rule (15 minutes), and a 12-bar position cap (60 minutes). This changes the physical context as well as confirmation resolution relative to M15; it is not a pure latency comparison.

TRAIN/DEVELOPMENT/FINAL sizes were 10,368 / 3,456 / 3,456 per market; required cycle counts remained 20/5/5. Risk remained 1% per trade, capped at 2x simulated notional with all existing fees, spread/slippage and estimated carry.

- Equilibrium target: zero trades on TRAIN. Receipt 63e137c18334cdb7e529a9b4e4e8d916165d72da8078a3850f7c5f7a20f76a54.
- Opposite-boundary target: two losing BTC trades, net -5.8705904 per 1,000 initial simulated units, including 4.3975070 fees and 0.0118041 estimated funding. One holding-cap exit and one stop. ETH and SOL stayed flat. Receipt ed08d78a8963033ea8dc1f374ad64cb1300fef5131a45372c5766e28d880f432.
- DEVELOPMENT and FINAL were never opened. Neither candidate was promoted. This does not demonstrate the requested 1% hourly return.
- New M5 data and protocols are retained at runtime/course-m5-confirmation-20261112. BTC dataset a36c3bf0e1098ab92ed46417632b5b56e5d5ec3452945b059b1c169beea886c2; ETH 3ba2f57bab2cda3b9ffa93f03bb1f495c2fcbfcd56c81015b30787d123675a53; SOL b07872a4459009f2d835b6614123e68860254404d6391000e5725c71aebf7f04.

Reproduce with the existing authenticated_bars and screen_universe helpers and RangeDeviationConfiguration(maximum_hold_bars=12, target_mode='EQUILIBRIUM' or 'OPPOSITE_BOUNDARY'). Do not count reused calendar history as new independent evidence.

Validation: nine focused tests and ten subtests passed; long/short target selection uses prior range bounds without changing stop or confirmation. No real order or paid model call.
