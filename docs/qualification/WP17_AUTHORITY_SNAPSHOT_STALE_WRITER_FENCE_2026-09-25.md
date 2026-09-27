# WP-17 authority snapshot stale-writer fence

Original qualified delta base: `main@7b8bd88aa09d32d3295b1f38465ec7fa7c73920b`.

The original reconvergence was rooted at `main@d1d6419daa905ca7d54db3461475cc5b7bf407a3`. The current clean successor is based on repaired `main@8e0e3d39aa9edc63c1cbf21de97b4caf2e15a7bc`; the owned WP-17 paths were unchanged across that base movement, so the successor preserves reviewed lineage without importing stale ancestry.

## Defects

The persistence adapter stores complete `AuthorityService` snapshots as immutable `financial-authority` journal events. Without a lineage-extension fence, two processes restored from the same prior snapshot can diverge: one can persist newer revocation/confirmation/admission facts while the stale process later appends an older complete state under a new event ID. Restart would then restore the later stale snapshot and forget durable authority facts.

A second fail-open path existed in `new_exposure_blocks`. Emergency no-new-risk state is changed by canonical `AuthorityNewExposureBlocked` / `AuthorityNewExposureRestored` events in `authority_state/canonical`, but block/restore does not advance the ordinary snapshot epoch. A stale or forged snapshot could therefore omit, rewrite, invent, or resurrect active block state unless snapshot publication was explicitly bound to that canonical authority.

For financial authority both defects are safety critical: a revocation, consumed confirmation, or active emergency exposure block must not be erased by stale snapshot publication or restart.

## Increment

Every genuinely new snapshot must monotonically extend the latest durable snapshot state:

- schema version cannot silently change inside one snapshot lineage;
- authority epoch cannot decrease;
- existing policy, revocation, confirmation, and admission records cannot disappear;
- an existing immutable record cannot be rewritten under the same identity;
- used confirmations are append-only and cannot be forgotten;
- malformed or duplicate snapshot collections fail closed.

Active no-new-exposure state is not treated as naively append-only because an exact canonical restore is legitimate. Instead the adapter performs a focused replay of only block/restore transitions from the existing `authority_state/canonical` journal, validating contiguous aggregate versions plus exact block/restore payload identity and timestamps. The candidate snapshot's active block map must equal that canonical replay exactly. This rejects stale omission, forged addition, identity rewrite, and resurrection after restore without introducing another authority. The focused replay deliberately avoids full financial-admission replay so emergency block persistence does not acquire an unrelated dependency on external evidence-artifact resolvers.

For a new snapshot, the adapter records the global journal sequence before reading durable snapshot/canonical-block evidence and commits the snapshot through the existing `JournalStore.commit_command(..., expected_journal_sequence=...)` transaction. Any intervening journal write invalidates the cut inside the write transaction, closing the proof-to-append race. The adapter maps that conflict into the same stale aggregate-version/journal-sequence failure class used by the existing stale-writer regression.

`restore_authority_snapshot()` also compares the restored snapshot block map with current canonical block/restore replay. A lagging snapshot therefore fails closed after a newly durable block or restore until a matching snapshot is published; it cannot silently resurrect new-risk eligibility or an already-restored obsolete block.

Immutable historical event-id retries remain lost-reply retries: they reproduce the original envelope and do not re-interpret later canonical state.

## Focused regressions

The current successor covers:

- stale writer attempting to erase a newer operator revocation;
- restart still observing the revocation and refusing dispatch;
- stale snapshot attempting to forget consumed-confirmation/admission history;
- interleaved newer snapshot invalidating the stale writer transaction;
- stale pre-block snapshot rejected even when no intermediate blocked snapshot exists;
- stale omission, forged addition, and active-block identity rewrite;
- exact durable restore, including fail-closed restart while the snapshot lags canonical restore state;
- a block appearing between canonical proof and snapshot append invalidating the global journal cut;
- historical blocked-snapshot lost-reply retry remaining idempotent after a later restore;
- failed stale publications leaving authoritative snapshot history unchanged.

## Qualification boundary

This increment hardens persisted operator authority and emergency no-new-exposure state. It does not itself establish provider/LIVE qualification, economic edge, release/accessibility readiness, or merge-time branch/rules enforcement. Fresh exact-head baseline, trusted reconvergence-integrity and dual-OS Verify AutoTrade remain mandatory before integration.

Broader operator workflow UI, autonomous-policy coverage and production qualification remain separate WP-17 work. Repository-level merge/current-base/required-check enforcement remains a separate control-plane gap tracked outside this increment.
