# Section 4 JournalStore closure — 2026-10-06

## Canonical result

Section 4 / WP-05 canonical JournalStore persistence authority is integrated on `main`.

- Current-main closure PR: #2196
- Accepted head: `5fa853a21f3e1ece525ecb891c3f54f317207f93`
- Exact base: `536002f84421eb073e267084ab60bd5829473485`
- Merge commit: `c0f929e25fbfc2df1f33b2746f5f486d9819ee74`
- Merge tree: `fa543248176fdecb83269b1d23bc09fc35443764`
- Changed paths: 10
- Topology before merge: ahead-only / behind 0 / mergeable.

## Persistence authority closed

The merged JournalStore now combines the previously integrated WP-05 inert/executable-ingress lineages with the remaining closure work reconstructed from canonical #2163 without rolling back Section-1 Host Sequence semantics.

Closure properties include:

- append-only journal + durable explicit global sequence;
- atomic event/outbox commit and atomic command/event/outbox transaction;
- command dedupe and durable effect identity;
- aggregate-version continuity and exact replay checks;
- one-snapshot journal-tail cursor validation;
- one-snapshot aggregate/global checkpoint validation;
- projection rebuild equivalence between full replay and checkpoint+tail after restart;
- crash-before/after-commit qualification across event/outbox, commands, checkpoints, schema migration, first-event claim and delivery acknowledgement;
- migration rollback/upgrade authority;
- exact durable text/storage-class validation for outbox, checkpoints and command results;
- canonical EventEnvelope v6 lexical/shape validation on persistence boundaries;
- canonical UiCommand validation before durable host mutation;
- exact idempotency-key identity without whitespace aliasing;
- exact built-in JSON/path/text/integer ingress before hashing or SQLite authority;
- first-event claim atomic ownership;
- physical JournalStore backing-file authority with canonical frozen path, exact JournalStoreIdentity, callback-safe raw-state validation, Windows retained-handle identity/fencing, hard-link/rebinding protection and restart-stable identity.

## Decisive regression surface

Merged exact-head regression surface includes at least:

- 74 core persistence tests;
- 9 first-event ownership tests;
- 33 executable/durable-ingress tests;
- 16 EventEnvelope contract persistence tests;
- 17 hard-process crash/restart tests;
- 9 snapshot/rebuild equivalence tests;
- 20 store-identity tests;
- 20 JournalStore path/physical-authority tests;
- host-network regression union preserving both Section-1 Sequence semantics and Section-4 UiCommand/idempotency semantics.

This explicitly covers the WP-05 acceptance criteria:
- committed records survive qualified crash boundaries;
- no partial journal/outbox/command transaction becomes durable;
- deterministic replay/checkpoint-tail rebuilds agree;
- changed-hash/idempotency conflicts fail closed;
- migration rollback/upgrade remains atomic;
- same-journal authority is physical/canonical rather than lexical-path-only.

## Lineage convergence

- #2034 is already an ancestor of main and supplied the inert persistence JSON authority.
- #2137 is merged and supplied the residual exact path/int/sequence ingress fences.
- #2163 was used as the canonical donor for the remaining snapshot/crash/EventEnvelope/outbox closure semantics.
- The #2196 successor merged those semantics onto current main while preserving newer Section-1 host Sequence behavior.
- Historical #1577/#1612/#2030/#2163 are superseded as Section-4 integration targets after this merge.

## Qualification boundary

GitHub-hosted exact-head jobs registered for #2196 but were still queued without runner assignment at the closure cut. Queued is not represented as PASS.

This record closes Section 4 / WP-05 source and canonical persistence integration. It does not claim provider/PAPER/LIVE authority, real-money safety, economic edge, signed release, physical Windows/NVDA qualification, or the broader WP-48/WP-49 restart/recovery program. Those remain separate gates.
