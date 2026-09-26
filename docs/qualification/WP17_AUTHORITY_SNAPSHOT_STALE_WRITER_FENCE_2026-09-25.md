# WP-17 authority snapshot stale-writer fence

Base: `main@7b8bd88aa09d32d3295b1f38465ec7fa7c73920b`.

## Defect

The existing authority persistence adapter stores complete `AuthorityService` snapshots as immutable journal events. Without a lineage-extension check, two processes restored from the same prior snapshot could diverge: one could persist a newer revocation/confirmation/admission fact, while the stale process could later append its older complete state under a new event ID. Restart would then restore the later stale snapshot and forget durable authority facts.

For financial authority this is fail-open: a revocation or consumed confirmation must never be resurrected by stale snapshot publication.

## Increment

The canonical persistence adapter now requires every new snapshot to monotonically extend the latest durable authority state:

- schema version cannot silently change inside one snapshot lineage;
- authority epoch cannot decrease;
- existing policy, revocation, confirmation, and admission records cannot disappear;
- an existing immutable record cannot be rewritten under the same identity;
- used confirmations are append-only and cannot be forgotten;
- malformed/duplicate snapshot collections fail closed.

The check is performed before a new authority event is appended. It does not create a second authority service, lock manager, or owner fence.

## Focused regressions

Tests cover:

- stale writer attempting to erase a newer operator revocation;
- restart still observing the revocation and refusing dispatch;
- stale snapshot attempting to forget consumed-confirmation/admission history;
- failed stale publication leaving the durable event count unchanged.

## Remaining WP-17 work

This increment hardens persisted operator authority state only. Broader operator workflow UI, autonomous-policy coverage, cross-process owner fencing, and production qualification remain separate gates.
