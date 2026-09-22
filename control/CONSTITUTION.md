# AutoTrade control constitution

## Objective
Minimize TIME_TO_WHOLE_FINISHED_AUTOTRADE.

## Canonical order
1. live reviewed implementation and evidence;
2. `control/INDEX.json`;
3. explicit supersession addenda;
4. approved engineering baseline;
5. historical snapshots.

## Concurrency
No global worker/auditor/PR cap. Exclusive mutation is defined by overlapping `AUTHORITY_FAMILY + SEMANTIC_KEY + MUTATION_SCOPE`.

## Bootstrap
Until the atomic claim registry exists, repository mutation remains single-writer. This is temporary bootstrap safety, not a permanent worker-count limit.

## Evidence
A document set, open PR, green unit suite, simulator result, or one successful trade is not whole-product completion.
