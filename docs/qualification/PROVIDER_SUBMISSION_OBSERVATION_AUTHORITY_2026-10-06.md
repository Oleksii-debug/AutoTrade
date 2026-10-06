# Provider submission observation authority — 2026-10-06

## Purpose

The provider-neutral `ProviderSubmissionObservation` is the consumer boundary
between durable send evidence and provider response normalization.

A durable `SubmissionResponseBinding` remains the restart-compatible financial
source of truth. This layer authenticates the in-process observation object so
that Python polymorphism, exact-instance forgery, or nested payload mutation
cannot masquerade as that durable evidence.

## Authority shape

The canonical factory now:
1. verifies the durable submission response binding and exact response bytes;
2. reparses those bytes through the bounded neutral JSON/numeric parser;
3. freezes the nested decoded payload;
4. registers the exact returned observation object in a closure-owned authority table.

The consumer verifier requires exact `ProviderSubmissionObservation` type and the
exact registered object identity. It rechecks all authority-bearing fields
against the issuance snapshot. The table is process-local by design; after a
restart a new observation can be reminted from the durable binding, so the
security property does not depend on a previous-process Python object surviving.

## Downstream boundary

Bybit submission normalization calls the canonical verifier before reading
response-binding properties, response identifiers or provider payload fields.
A forged subclass therefore cannot execute an overridden property before the
authority decision, and an exact `object.__new__` clone cannot become a financial
response.

## Deliberately not added

This change does not create a new dispatcher, retry authority, reconciliation
ledger, provider qualification, credential path, provider transport, or
profitability/economic-edge claim. UNKNOWN outbound state remains governed by
the durable send/reconciliation path.

## Verification boundary

The branch adds adversarial regressions for:
- uninitialized exact-object clones;
- caller subclasses with hostile properties;
- post-issuance mutation of authority-bearing fields;
- nested payload mutation attempts.

Local clone/test execution is unavailable in the current container because
github.com DNS resolution fails. Hosted exact-head checks remain the terminal
verification requirement.
