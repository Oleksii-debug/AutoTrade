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
