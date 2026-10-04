# Section 11 corporate actions and financing closure candidate

Date: 2026-10-04
Base main: `cc0b7fa5cee4597dc8039f0b0a5fd622e8b12a29`

Current main already contains durable financing, financing revisions/corrections, corporate-action entitlement authority and authenticated borrow evidence. This candidate closes the remaining provider-neutral corporate-equity arithmetic/trust gaps without creating a second economic engine.

## Residuals closed

- split/dividend/merger/delist and related cash/unit-basis arithmetic use shared bounded exact arithmetic rather than ambient Decimal context;
- durable CASH_DIVIDEND reconstruction and delta/opposite-income postings use canonical exact values;
- non-terminating projections fail closed where no rounding policy exists;
- oversized numeric payloads fail through the shared resource envelope before process-global integer/string limits;
- event/state/checkpoint/book/helper ingress requires exact canonical types before semantic reads;
- activation/entitlement time uses exact built-in datetime/fixed timezone authority;
- accepted corporate-event payloads are detached from caller mutation;
- unsupported durable corporate-action kinds remain fail closed.

## Already integrated and preserved

- durable financing + revision/correction authority;
- corporate-action entitlement cut + JournalStore authority;
- canonical exact accounting and settlement;
- securities-borrow evidence.

## Closure requirements

Section 11 is DONE only after exact-head qualification proves:
1. supported corporate events create exact, conserved economics;
2. corrections/revisions are idempotent and restart-safe;
3. financing revisions conserve cash/P&L across restart;
4. malformed/polymorphic input cannot become financial authority;
5. baseline and Verify are terminal green;
6. review state is clean;
7. merge and post-merge readback confirm accepted source identity.

Real provider-origin issuance remains a deferred provider qualification concern and is not claimed here.

## Follow-up source review: authoritative action issuance

Exact source review after convergence found that frozen=True plus an exact AuthoritativeCorporateAction type was insufficient authority: callers could manually reconstruct the dataclass (or mutate a legitimately resolved instance with object.__setattr__) and DurableCorporateActionEvidenceStore would persist its fields without proving resolver issuance.

The canonical Section 11 candidate now also:
- seals resolver-issued AuthoritativeCorporateAction instances in process state outside caller-writable dataclass fields;
- retains an immutable canonical snapshot and returns a detached snapshot to durable/accounting consumers;
- rejects manually reconstructed exact actions and post-resolution mutation;
- enforces the issuance seal before financial code reads observed_at or other accepted fields;
- rejects hostile str, InstrumentRegistry subclasses and ProviderResponseObservation subclasses at the corporate-action evidence boundary;
- includes durable-no-mutation regressions for the above paths.

This does not claim external provider-origin issuance is globally solved. The provider-core/real-provider qualification remains a separate gate; this repair only prevents the corporate-action durable/financial path from treating arbitrary self-minted AuthoritativeCorporateAction DTOs as resolver-issued evidence.
