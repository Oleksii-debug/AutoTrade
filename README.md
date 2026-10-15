# AutoTrade

## Historical provider-free campaign — new Plan 10 (2026-10-10)

[Canonical Plan 10 in Google Drive](https://docs.google.com/document/d/1bTDCb0yGLrOqlKClhyAD_7k7aZsP57kFRkexHz_b6d4/edit): actual 2024+ lawful public BTCUSDT/ETHUSDT market history intake, no-lookahead causal replay, $1000 USD ZERO virtual account, separate data collector/simulated broker/trader/learner and independent walk-forward evaluation. Source-only engineering can start immediately with **no broker accounts or API keys**. Until next-bar execution, historical data, model training and independent evaluation are demonstrably integrated, the existing example commands below are **synthetic development simulations, not historical trading performance proofs**. Terminal Plans 1–8 remain DONE. Plan9 retains production signing/legal/NVDA gates and optional real provider/PAPER/LIVE tracks.


### Plan 10 Section 1: public archival ingestion (not a trading provider)

Python 3.12, no broker account, exchange API key, paid API, or wallet is needed.
Run at repository root with a **completed and published** monthly end date:

```console
python -m tools.historical_public_klines --symbols BTCUSDT,ETHUSDT --interval 1h --from-month 2024-01 --through-month 2026-09 --output historical-data
# Optional research-only daily intake after the completed monthly cut (example as of 2026-10-10):
python -m tools.historical_public_klines --symbols BTCUSDT,ETHUSDT --interval 1h --from-month 2024-01 --through-month 2026-09 --daily-through 2026-10-08 --output historical-data
python -m unittest discover -s tests/History -v
```

The collector checks each official Binance Vision monthly ZIP against its publisher SHA-256 CHECKSUM. It verifies spot OHLCV, UTC hourly continuity, publisher timestamp units (milliseconds before 2025; microseconds after), rejects unsafe archive members, and publishes immutable normalized CSV plus a provenance manifest. A missing file, gap, mismatched digest, or revised archived bytes **fails closed**: it is never invented or replaced. Files under `historical-data/` contain downloaded data, not code; keep them outside committed source control. Each dataset manifest records the observed download instant; `published_at_utc: null` means the publisher's exact original publication time was not verified.

**Source rights:** Binance Vision Dataset Terms version dated 2026-08-26, available at
https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md ,
restrict the default dataset license to **CC BY-NC-SA 4.0 plus non-commercial-only terms**.
This intake is strictly personal non-commercial research/simulation; do **not** redistribute the raw archive, use it for commercial products or connect it to compensated signal distribution or LIVE execution without appropriate separate rights.

The original source URL, publisher checksum SHA-256, normalized CSV SHA-256, exchange, symbol, interval, date range, gap count, and research-only rights notice are kept in each manifest. This does **not** qualify profitability or a public signed release. The default `tools/run_historical_zero.py` experiment remains **SAME-CLOSE / INTERNAL / UNQUALIFIED**. An opt-in next-open cost scenario and a causal parameter-training candidate exist on PR #2389; neither closes Plan 10 Sections 2–6 or establishes economic edge.

### Experimental training and independent historical run

After collecting completed January through March 2024 BTCUSDT hourly archives as above, train on January's closed-bar labels, score the unchanged model on February, and execute an independent March holdout using the **existing** ZERO risk, OMS, simulated provider and financial journal. Use a separate state directory for each account. The archived source files, validation reports, models and account state must remain outside Git.

```console
python -m tools.train_historical_agent --data-root historical-data --symbol BTCUSDT --interval 1h --from-month 2024-01 --through-month 2024-01 --model-out local-models/btc-2024-01.json
python -m tools.validate_historical_agent --data-root historical-data --symbol BTCUSDT --interval 1h --from-month 2024-02 --through-month 2024-02 --model-path local-models/btc-2024-01.json --report-out local-models/btc-validation-2024-02.json
python -m tools.run_historical_zero --data-root historical-data --symbol BTCUSDT --interval 1h --from-month 2024-03 --through-month 2024-03 --next-bar-open --model-path local-models/btc-2024-01.json --validation-report-path local-models/btc-validation-2024-02.json --virtual-cash 200 --state-dir local-state/btc-200-march --run-id march-200
python -m tools.run_historical_zero --data-root historical-data --symbol BTCUSDT --interval 1h --from-month 2024-03 --through-month 2024-03 --next-bar-open --model-path local-models/btc-2024-01.json --validation-report-path local-models/btc-validation-2024-02.json --virtual-cash 1000 --state-dir local-state/btc-1000-march --run-id march-1000
```

These are developer commands, not a ready Windows installer. A completed month can be processed in bounded pieces with `--stop-after-episodes N`; repeat with a larger N and the same state/run/model/validation to resume. The training command does not create orders. It updates candidate weights only when the following candle has closed; the selected parameters are frozen during validation and independent holdout. Validation records directional accuracy, not trading P&L; its hash is bound into the holdout protocol, and a changed, corrupted, mismatched or overlapping report is rejected before creating financial state. Changing the training or validation period, model, costs, or code identity in an existing state fails closed. The next-open run assumes the prior candle was available at its close, adds a fixed adverse 10 basis point scenario plus the existing 10 basis point provider fee, and has no observed order-book liquidity. The JSON `comparative_report` shows the completed prefix's canonical agent equity/P&L/drawdown alongside cash and hypothetical full-cash buy-and-hold liquidation with these fixed costs. Negative P&L remains a valid result. The model and historical dataset are not a production promotion artifact; Windows packaging, full independent holdout qualification, long-lived online learning and human NVDA verification remain open.

For a single restartable research command that executes the same three cuts and **both separate virtual accounts** through the existing engine, run:

```console
python -m tools.run_historical_campaign --data-root historical-data --workspace local-state/campaign-btc --symbol BTCUSDT --train-from 2024-01 --train-through 2024-01 --validate-from 2024-02 --validate-through 2024-02 --holdout-from 2024-03 --holdout-through 2024-03 --episode-limit 24
```

The command prints a short JSON account comparison and writes immutable model, validation and completed-prefix report files under `--workspace`. Repeat it with the same episode limit to verify recovery, then raise `--episode-limit` or omit it to continue through the complete period. The $200 and $1000 accounts have separate journals. Training and validation must succeed before any account is opened; if one account fails, the command does not publish a combined report and an identical retry recovers the other account from its own journal. This is one finite, frozen-policy research cut, **not** continuous online learning, a Windows user interface or an economically qualified strategy.

The per-account `comparative_report` also contains a reconciled hourly equity curve, sample hourly return volatility, an unannualized zero-cash-rate Sharpe calculation when variance is nonzero, and a **mark-based position-turnover proxy**. These statistics read completed simulated episodes only. They are not exchange execution measurements or proof of an advantage; very short prefixes are particularly unstable.

To inspect successive model versions and results, use a bounded monthly walk-forward cut after collecting all named months. This freezes each model for its next validation and holdout, then allows a later month to train on the previously closed period. Repeating the command resumes the same journals and immutable artifacts:

```console
PYTHONPATH=research:. python -m tools.run_historical_walkforward --data-root historical-data --workspace local-state/walkforward-btc --symbol BTCUSDT --train-from 2024-01 --first-holdout 2024-03 --last-holdout 2024-04 --max-cycles 2
```

The JSON lists the $200 and $1000 balances, risk and benchmark for each monthly cut, with the model and validation hashes. This finite retrospective research loop does not run in the background or promote a strategy to provider trading. The original publication time of archived data is not established, so it is not a point-in-time live trading or profitability claim.



Universal autonomous multi-agent financial trading platform.

## Run the network-free simulation

From the repository root, Python 3.12 can run one journal-backed BUY/HOLD episode without provider accounts or model calls:

```console
python -m mvp.autotrade_mvp.cli --canonical-simulation --state-dir simulation-state --episode-id example-1 --prices 100,101,103
python -m mvp.autotrade_mvp.cli --state-dir simulation-state --accessible-status
python -m mvp.autotrade_mvp.cli --state-dir simulation-state --economic-report
python -m mvp.autotrade_mvp.cli --state-dir simulation-state --history --history-limit 20
```

Repeating the first command resumes the same episode without a new submission. Use a separate directory for a different episode or the legacy multi-episode demo. `--status` returns JSON; `--accessible-status` returns plain, copyable text with units and required recovery actions. All read commands leave financial events, outbox and reservations unchanged. Old journal schemas require an explicit migration rather than an upgrade during status reading.

To exercise a lost response, use a new directory and add `--fault-after-send` to the simulation command. The durable outcome remains UNKNOWN, retains reserved cash and requires reconciliation; reading or restarting never retries the send. A reconciled fill also remains distinct from a confirmed terminal order. Reports show recorded cash, fees and turnover; an open position without a retained market mark has unavailable equity/P&L. These are simulation facts, not profitability or release qualification.

## Canonical entry points

- `control/INDEX.json` — live repository control entry point.
- `docs/product/PRODUCT_SPEC_CANONICAL.txt` — machine-readable canonical product specification mirror.
- `docs/product/AutoTrade_Final_Product_Specification_EN.docx` — source document snapshot.
- `docs/engineering/00_AUTOTRADE_MASTER_ENGINEERING_SPEC.md` — master engineering baseline.
- `docs/engineering/02_CANONICAL_CONTRACTS.md` — canonical contract design.
- `contracts/jsonschema/` — materialized language-neutral contract schemas.
- `contracts/openapi/host-api.yaml` — canonical host/UI API entrypoint.
- `control/work-packages/bank.json` — machine-readable bank of 65 implementation packages.
- `docs/engineering/12_UNIVERSAL_WORKER_PROMPT.md` — stable implementation-worker instruction.
- `docs/engineering/13_UNIVERSAL_AUDITOR_PROMPT.md` — stable auditor instruction.

## Bootstrap state

The architecture/product transfer baseline is complete. Implementation is **not** complete.

Already materialized:
- engineering documents 00–14;
- product specification;
- canonical control issues and control index;
- dedicated claim-registry branch;
- selected neutral Autosport primitives;
- Nika model-gateway contract semantics;
- canonical JSON Schema/OpenAPI surface;
- .NET 10 contract foundation;
- dependency/provenance inventory;
- adapted fail-closed swarm lease/collision invariants.

Concurrent source mutation remains disabled until the registry/CAS path has exact-head CI and integration qualification.

A document set, green unit suite, simulator result or one successful trade is not whole-product completion. Economic edge remains unproven until causal/forward evidence exists.

## Start implementation from the current baseline

Read [the implementation start guide](docs/engineering/15_FAST_IMPLEMENTATION_START.md) and [the finalization audit](docs/engineering/16_BASELINE_FINALIZATION_AUDIT.md). Install the pinned development requirements, then run `python tools/verify.py`. Update package fields only in `control/work-packages/bank.json` and run `python tools/baseline.py refresh`.

Export an exact committed baseline with `python tools/baseline.py pack` after fetching registry refs. The exporter includes documents 00–16, product and source files, accessible HTML, registry snapshot, source revision and SHA-256 manifest. Generated archives are historical snapshots; live GitHub state may advance.
