# AutoTrade control constitution

## Objective
Minimize TIME_TO_WHOLE_FINISHED_AUTOTRADE.

## Canonical order
1. approved product and engineering contracts define intended behavior;
2. reviewed implementation and exact-source evidence establish actual behavior;
3. `control/INDEX.json` resolves current bank, provenance and qualification;
4. export source revision and manifest identify the exact snapshot;
5. historical snapshots never silently override current decisions.

A code defect does not supersede a requirement. Resolve discrepancies explicitly.

## Concurrency
No global worker/auditor/PR cap. Exclusive mutation is defined by overlapping `AUTHORITY_FAMILY + SEMANTIC_KEY + MUTATION_SCOPE`.

## Bootstrap
Until the atomic claim registry exists, repository mutation remains single-writer. This is temporary bootstrap safety, not a permanent worker-count limit.

## Evidence
A document set, open PR, green unit suite, simulator result, or one successful trade is not whole-product completion.
