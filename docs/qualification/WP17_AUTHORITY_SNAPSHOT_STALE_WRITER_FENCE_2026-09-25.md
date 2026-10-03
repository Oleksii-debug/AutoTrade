# WP-17 authority snapshot stale-writer fence

Original qualified delta base: `main@7b8bd88aa09d32d3295b1f38465ec7fa7c73920b`.

The original reconvergence was rooted at `main@d1d6419daa905ca7d54db3461475cc5b7bf407a3`. This successor was rebuilt from repaired `main@8e0e3d39aa9edc63c1cbf21de97b4caf2e15a7bc`. At the latest source review, live main had advanced to `595ed24fe4244f05d722eae570b27e46bf976f04` only through unrelated WP-04 strict-JSON paths; the WP-17 owned paths were unchanged. Final integration still requires a fresh live-main ancestry/convergence check.

## Defects

The persistence adapter stores complete `AuthorityService` snapshots as immutable `financial-authority` journal events. Without a lineage-extension fence, two processes restored from the same prior snapshot can diverge: one can persist newer revocation/confirmation/admission facts while the stale process later appends an older complete state under a new event ID. Restart would then restore the later stale snapshot and forget durable authority facts.

The first stale-writer fence compared a candidate only with the preceding `financial-authority` snapshot. That is insufficient when canonical `authority_state/canonical` advances without an intermediate snapshot. A concrete falsifier is S0 -> canonical `AuthorityPolicyRevoked` -> stale S0 publication: previous snapshot and candidate can still be identical, and the global-sequence compare-and-append does not reject a canonical event that already existed before the captured cut. The latest snapshot could then resurrect an unrevoked policy on restart.

The same no-intermediate-snapshot class applies to canonical confirmation/admission/used-confirmation progression. A stale snapshot must not make a durably consumed confirmation reusable merely because no snapshot was published between canonical admission and the stale publication.

A separate fail-open path existed in `new_exposure_blocks`. Emergency no-new-risk state is changed by canonical `AuthorityNewExposureBlocked` / `AuthorityNewExposureRestored` events in `authority_state/canonical`, but block/restore does not advance the ordinary policy/revocation epoch. A stale or forged snapshot could therefore omit, rewrite, invent, or resurrect active block state unless snapshot publication was explicitly bound to canonical authority.

For financial authority all of these defects are safety critical: canonical revocation, confirmation consumption, admission history, or active emergency exposure block must not be erased or invented by snapshot publication or restart.

## Increment

Every genuinely new snapshot still has to monotonically extend the latest durable snapshot lineage:

- schema version cannot silently change inside one snapshot lineage;
- authority epoch cannot decrease;
- existing policy, revocation, confirmation, and admission records cannot disappear;
- an existing immutable record cannot be rewritten under the same identity;
- used confirmations cannot be forgotten;
- malformed or duplicate snapshot collections fail closed.

That snapshot-to-snapshot fence is now only a first line of defense. If `authority_state/canonical` contains any event, the complete candidate snapshot must also equal the complete deterministic state projected from that canonical aggregate at the captured journal cut: schema/epoch, policies, revocations, confirmations, admissions, used confirmations, and active new-exposure blocks.

The projection does **not** implement a second transition parser or a weakened financial replay mode. It instantiates the ordinary canonical `AuthorityService` on the same `JournalStore`, so `_restore_journal()` performs the existing journal-order, policy/revocation, confirmation/admission, policy-scope, used-confirmation, block/restore and financial-evidence checks. A durable admitted financial record still has to match its risk decision, authoritative risk snapshot, reservation identity/delta, reconciliation availability evidence, journal cut and request fingerprint. A deliberately malformed canonical admission with no referenced risk-decision event is therefore rejected before snapshot publication.

CASH financial evidence is reconstructed entirely from durable journal state. BORROW evidence also has a cryptographic artifact boundary in the canonical authority implementation. Snapshot publication reuses the candidate service's trusted `evidence_artifact_store`; snapshot restore accepts the same optional `ArtifactStore` explicitly. A canonical BORROW history without that trusted artifact authority fails closed rather than silently weakening replay. This is intentional: snapshot proof is not allowed to make securities-borrow evidence less strict than dispatch/financial authority.

The adapter additionally rejects a durable `AuthorityService` bound to a different `JournalStore` than the target snapshot store. Candidate state, canonical projection and the compare-and-append cut must belong to one durable authority domain.

Active no-new-exposure state remains exactly bound to canonical block/restore history. The adapter preserves specific fail-closed diagnostics for stale omission, forged addition, identity rewrite, and resurrection after restore.

A deliberately narrow legacy boundary remains: if the canonical authority aggregate is genuinely absent, historical snapshot-only state remains readable/publishable under the existing monotonic snapshot lineage rules. Active `new_exposure_blocks` are not permitted in that mode because their authority is the canonical block/restore journal. Once any canonical authority event exists, full snapshot equality to canonical projection is mandatory; canonical and snapshot-only authority are not allowed to diverge thereafter.

For a new snapshot, the adapter records the global journal sequence before reading previous snapshot/canonical evidence and commits through the existing `JournalStore.commit_command(..., expected_journal_sequence=...)` transaction. Any intervening journal write invalidates the cut inside the write transaction. Together with full canonical projection, this closes both sides of the stale window: canonical mutations already present before the cut must be represented in the candidate, and mutations after proof invalidate append.

`restore_authority_snapshot()` revalidates the complete restored snapshot against current canonical projection whenever canonical authority exists. A lagging snapshot therefore fails closed after revocation, admission/confirmation consumption, block, or restore until a matching snapshot is durably published.

Immutable historical event-id retries remain lost-reply retries: they reproduce the original envelope and do not reinterpret later canonical state.

## Focused regressions

The current successor covers:

- canonical revocation after S0 with no intermediate snapshot: stale S0 publication fails before journal mutation;
- the same lagging S0 snapshot fails restart after canonical revocation;
- canonical SIMULATION admission consuming a confirmation after S0 with no intermediate snapshot cannot be erased or made reusable by stale publication;
- once a matching canonical snapshot is published, restart preserves the consumed confirmation and rejects reuse;
- a snapshot cannot invent an additional confirmation/fact once canonical authority exists;
- malformed canonical transition history is rejected by the shared `AuthorityService` replay instead of being accepted by a separate projector;
- malformed admitted financial canonical history with missing risk-decision evidence is rejected, proving snapshot projection does not bypass durable financial-evidence validation;
- a service bound to another JournalStore cannot publish into the target snapshot store;
- existing snapshot-lineage stale revocation/history-loss and interleaving CAS regressions;
- stale pre-block snapshot rejected even when no intermediate blocked snapshot exists;
- stale block omission, forged addition, and active-block identity rewrite;
- exact durable restore, including fail-closed restart while the snapshot lags canonical restore state;
- a block appearing between canonical proof and snapshot append invalidating the global journal cut;
- historical blocked-snapshot lost-reply retry remaining idempotent after a later restore;
- failed stale/forged publications leaving authoritative snapshot history unchanged.

## Qualification boundary

This increment hardens persisted operator/financial authority consistency. It does not itself establish provider/LIVE qualification, economic edge, release/accessibility readiness, or merge-time branch/rules enforcement. Fresh exact-head focused tests, baseline, trusted reconvergence-integrity and dual-OS Verify AutoTrade remain mandatory before integration, followed by a fresh current-main ancestry check.

Broader operator workflow UI, autonomous-policy coverage and production qualification remain separate WP-17 work. Repository-level merge/current-base/required-check enforcement remains a separate control-plane gap tracked outside this increment.
