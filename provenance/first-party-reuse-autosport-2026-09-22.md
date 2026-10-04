# First-party reuse: Autosport → AutoTrade

Source snapshot: `Oleksii-debug/Autosport@cb102d85f0c820c7097875191deca73e53ec94f5`.

This migration is intentionally narrow and domain-neutral.

## Imported / adapted

1. Strict JSON behavior from `src/autosport/json_integrity.py`.
   - Destination: `research/autotrade_research/io/strict_json.py`.
   - Sports event schemas were not copied.

2. Durable publication mechanics characterized from `src/autosport/integrity.py`.
   - Destination: `research/autotrade_research/artifacts/durable_publish.py`.
   - Autosport scientific-registry authority logic was deliberately not copied.
   - AutoTrade adds POSIX parent-directory fsync after replacement.

3. Local workspace lock concepts from `src/autosport/workspace_lock.py`.
   - Destination: `research/autotrade_research/artifacts/resource_lock.py`.
   - Renamed and constrained to local research/artifact coordination.
   - It is explicitly forbidden as a distributed or financial execution fence.

## Source characterization read

- `tests/test_replay_jsonl_integrity.py`
- `tests/test_integrity_atomic_write.py`
- `tests/test_durable_path_lock.py`
- `tests/test_workspace_economic_lock.py`

## Rights status

The migration is performed under the repository owner's explicit instruction. The earlier engineering audit did not independently establish a complete root license/contributor-rights chain for all first-party repositories. Distribution/release qualification must still record the final rights basis and notices; this commit does not silently convert that open item into a license conclusion.

## Local migration evidence

Before publication, a focused neutral suite was run outside GitHub against these migrated implementations: 6 tests passed (strict duplicate/nonstandard/oversized integer rejection, JSON whitespace handling, stable atomic JSON publication, and cross-process resource-lock contention). This is migration evidence only; Windows CI and the broader WP-04 acceptance suite remain required.
