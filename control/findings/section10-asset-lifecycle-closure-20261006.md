# Section 10 provider-free asset lifecycle closure — 2026-10-06

## Canonical result

Section 10 provider-free asset lifecycle authority is closed on `main`.

- Final closure PR: #2286
- Accepted head: `ef7925ba3460d3ce684f6ed5dcadf4c071f6afb3`
- Accepted base: `e27dd1a3a27bee1084d19912b974d0112930f9a4`
- Merge commit: `4d5ad9f94fee81622b88257f3e1ac931fff1575d`
- Accepted candidate tree: `19def0fcad934a0036646218c9e8f856eaf7ff69`
- Post-merge main tree: `19def0fcad934a0036646218c9e8f856eaf7ff69`
- Post-merge tree equality: PASS

This closes the remaining provider-free FUTURE lifecycle authorization gap without creating a second instrument registry, risk engine or financial authority.

## Current-main convergence

The merged result preserves the current Section-5 instrument/settlement authority and adds the reviewed Section-10 safety lineage from #1622 without replaying its stale ancestry.

Closed boundaries:
- the caller-authored `physical_delivery_authorized` boolean is removed;
- PHYSICAL futures are hard `DELIVERY_BLOCKED` at/after the delivery cutoff;
- exact `FuturesContract` construction lifecycle identity is retained outside caller-writable object fields;
- missing, mutated, forged, subclassed or stale lifecycle/instrument state fails closed;
- `lifecycle_gate` re-resolves the exact version from `InstrumentRegistry`, requires the version to be effective/current and preserves instrument status;
- a caller-created registry remains diagnostic only in the public helper and cannot authorize new exposure.

## Positive product-owned admission authority

The missing positive composition is now owned by the existing financial writer:
- `AuthorityService` selects one exact `InstrumentRegistry` at construction;
- the selected registry is retained in closure-owned process binding with mutation/lifetime checks, matching existing JournalStore/capital composition patterns;
- `AuthorityService.admit()` accepts no per-call registry or delivery-authorization argument;
- after the authoritative risk snapshot and risk decision, an admitted non-reduce-only FUTURE `ORDER.SUBMIT` re-resolves the exact `instrument_id@version` from the service-owned registry;
- the selected version must be exact FUTURE authority for the same provider;
- the service rebuilds the canonical `FuturesContract` and requires `lifecycle_gate == OPEN` at the exact evaluated instant before reservation/financial admission mutation;
- an exact protective `reduce_only` admission is not blocked by the new-exposure lifecycle gate; false reduce-only intent remains rejected by the existing risk engine.

## Regressions

The accepted result includes regressions for:
- legacy physical-delivery boolean removal;
- forged/mutated/unbound contract state;
- wrong, stale, inactive, subclassed and caller-minted registry selections;
- service-owned registry positive admission;
- missing service registry fail-closed before reservation;
- PHYSICAL delivery cutoff fail-closed before reservation;
- protective reduce-only exit after cutoff.

## Qualification boundary

Provider-free source/integration lifecycle authority is merged and read back closed. It does not enable physical delivery and does not claim provider/PAPER/LIVE/real-money/profitability/release/NVDA qualification. Queued/pending/cancelled hosted CI is never represented as PASS.


## Residual WP-28 boundary

Broader WP-28 provider contract metadata, exchange-calendar/roll and provider qualification remains separately IN_PROGRESS. That provider-specific work does not reopen the closed provider-free Section-10 source/integration lifecycle authority.
