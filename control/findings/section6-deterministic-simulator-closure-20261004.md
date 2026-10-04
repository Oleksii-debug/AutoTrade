# Section 6 deterministic simulator closure candidate

Date: 2026-10-04
Base main: `cc0b7fa5cee4597dc8039f0b0a5fd622e8b12a29`

Current main already contains canonical simulation ownership/recovery, deterministic protocol identity, atomic OMS/finance/settlement convergence, partial fills, and crash-safe ZERO continuation. This candidate closes the remaining demonstrated provider-free simulator determinism/trust gaps.

## Residuals closed

1. Simulation policy chronology
- SIMULATION policy registration uses the frozen simulation timestamp rather than physical process time;
- uninterrupted vs pause/resume durable policy events can remain identical;
- non-SIMULATION scopes cannot consume simulation_time.

2. Execution DTO/scalar authority
- simulator and independent conservative oracle accept exact canonical DTO types;
- caller-owned frozen dataclass instances are detached/reconstructed before authority-bearing use;
- hostile Decimal/string/integer subclasses and post-construction mutation fail closed;
- exact bounded numeric primitives remove ambient Decimal-context authority.

3. MARKET price projection
- MARKET execution uses a versioned price-projection policy bound to InstrumentVersion.price_tick;
- BUY rounds adversely upward and SELL downward to the declared instrument quantum;
- independent oracle reconstructs and verifies the same adverse tick bound;
- mismatched instrument grid/policy fails qualification.

4. Execution qualification evidence
- qualification reads immutable evidence through one authenticated ArtifactStore snapshot and verifies the exact bytes/digest.

## Closure requirements

Section 6 is DONE only after:
1. focused simulation/execution qualification passes on the exact head;
2. full baseline and Verify are terminal green;
3. review state is clean;
4. one integrated provider-free simulation scenario remains deterministic across restart;
5. merge completes;
6. post-merge readback confirms accepted source identity.

No provider/PAPER/LIVE, profitability, economic-edge, signed-release or NVDA qualification is granted.
