# SOL trend forward-shadow retirement — 2026-11-12

## Decision

The `sol-trend-forward-shadow` candidate is retired from the durable Windows
runtime manifest. Its historical artifacts, live checkpoint, fills, diagnostics
and model configuration remain preserved for audit and later comparison.

This is a simulation/research decision. It sends no exchange order, grants no
trading authority and makes no profitability claim.

## Reason

The exact configuration used by the forward shadow was re-evaluated over four
contiguous, non-overlapping 4,000-hour SOL_USDT windows. Every window used its
own 2,400-hour TRAIN, 800-hour DEVELOPMENT and 800-hour FINAL partitions with
next-bar execution, 10 bps fee, 10 bps spread, 5 bps slippage, 2 bps/day short
carry, a minimum of three closed cycles and a 25% maximum drawdown gate.

Dataset identity:
`2fadbae1ac0511958467024398c9925c9fc45953a1a28309d08d5080d8a6975c`.

| Window | UTC interval | Gate result | Exact result SHA-256 |
| --- | --- | --- | --- |
| 1 | 2025-01-14 04:00 to 2025-06-29 20:00 | TRAIN drawdown limit exceeded | `cd2ed8eae54eb85a39dbebdff7ed220f8ce657d79516996f91f16b875ff80ceb` |
| 2 | 2025-06-29 20:00 to 2025-12-13 12:00 | TRAIN did not beat CASH and HOLD | `5fe06056737159ce907149f55134530f6a5652eb4b474cac0c590fd7233a3bd4` |
| 3 | 2025-12-13 12:00 to 2026-05-29 04:00 | TRAIN did not beat CASH and HOLD; drawdown limit exceeded | `da362520a104cee362a5ed010f4944c0dd2a4cb37e0ec3b8f9eba215a4f31eca` |
| 4 | 2026-05-29 04:00 to 2026-11-11 20:00 | TRAIN did not beat CASH and HOLD | `4362177f08a45e60e96220e420f943dd385b01890ce39de37f4f90b33ff894c2` |

No FINAL partition was consumed because no window passed its causal TRAIN gate.
The robustness score is therefore **0/4 qualified windows**. Continuing this
candidate as a durable forward process would spend compute on a configuration
that did not reproduce its earlier favorable regime.

## Runtime effect

The public SOL collector and the independent SOL live learner continue running.
Only this fixed trend forward-shadow process is removed. The watchdog remains
responsible for every other registered collector, learner, diagnostics process,
advisory process and research supervisor.

