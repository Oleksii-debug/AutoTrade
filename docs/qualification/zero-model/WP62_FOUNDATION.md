# WP-62 zero-model qualification foundation

This evidence path verifies one narrow but mandatory failure mode: language-model
inference is unavailable and AutoTrade still preserves a deterministic, network-free
simulation/replay/economic-reporting path.

The qualification deliberately uses `RoutingMode.ZERO`. A passing run requires:
- no admitted model or provider identity;
- exactly zero reserved model cost;
- ZERO routing must not even iterate the unavailable model inventory, excluding a
  hidden local/remote fallback path at this admission boundary;
- an unavailable remote route to fail with `NO_MODEL`, zero reserved cost and no
  fallback identity;
- a synthetically overloaded local route (latency beyond the admitted resource
  envelope) to fail with `NO_MODEL`, zero reserved cost and no remote fallback;
- the deterministic financial slice to resume without a duplicate order, fill, or
  learning-evidence row;
- a two-episode deterministic BUY→SELL campaign to survive restart/replay with the
  same order/fill identities, two immutable evidence records, exact fee/P&L
  reporting and a flat ending position;
- replay and reconciliation to remain true;
- the economic report to remain `UNPROVEN_SIMULATION_ONLY`;
- a small-capital run to be risk-rejected before order/fill creation, remain
  restart/replay safe, and still produce a reconciled zero-trade economic report;
- evidence to name the exact canonical lowercase 40- or 64-character Git object id.

The workflow runs the same exact-head qualification on both `ubuntu-latest` and
`windows-latest`, and publishes OS-distinct `zero-model-evidence.json` artifacts. Each evidence JSON also
records the runtime-observed operating system and Python implementation/version,
so the artifact remains platform-identifiable after download. This cross-platform evidence checks deterministic no-model behavior on the supported
Windows execution substrate as well as Linux; it is qualification evidence, not
financial authority.

## Explicit unresolved scope

This foundation does **not** complete WP-62. The remote outage is a routing-level
absence of the configured remote descriptor, and local resource exhaustion is a
deterministic routing-level overload fixture; neither is a measured operating-system
or provider outage. It does not yet prove durable research-job continuity,
paper-trading host/UI continuity, real provider minimums/lot-size economics, or
authority workflows during a real local/remote model outage. The small-capital
check is only the deterministic simulator's insufficient-cash boundary. The
two-episode campaign proves model-free software/economic continuity, not economic
edge, and performs no live provider call. Full WP-62 remains blocked until the
accepted WP-39, WP-40 and WP-55 artifacts exist and an end-to-end outage
qualification is run against them.
