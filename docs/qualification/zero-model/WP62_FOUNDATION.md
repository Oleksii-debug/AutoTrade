# WP-62 zero-model qualification foundation

This evidence path verifies one narrow but mandatory failure mode: language-model
inference is unavailable and AutoTrade still preserves a deterministic, network-free
simulation/replay/economic-reporting path.

The qualification deliberately uses `RoutingMode.ZERO`. A passing run requires:
- no admitted model or provider identity;
- exactly zero reserved model cost;
- the deterministic financial slice to resume without a duplicate order, fill, or
  learning-evidence row;
- replay and reconciliation to remain true;
- the economic report to remain `UNPROVEN_SIMULATION_ONLY`;
- evidence to name the exact 40-character source commit SHA.

The workflow writes `zero-model-evidence.json` as an exact-head artifact. The
artifact is qualification evidence, not financial authority.

## Explicit unresolved scope

This foundation does **not** complete WP-62. It does not prove research-job
continuity, paper-trading host/UI continuity, small-capital provider minimums, or
authority workflows during a real local/remote model outage. It does not establish
economic edge and performs no live provider call. Those claims remain blocked until
the relevant WP-39, WP-40 and WP-55 integrations are accepted and an end-to-end
outage qualification is run against them.
