# AutoTrade

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
