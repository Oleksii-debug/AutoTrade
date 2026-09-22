# AutoTrade

Universal autonomous multi-agent financial trading platform.

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
