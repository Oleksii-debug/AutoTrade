# WP-25 — Binance USD-M canonical identity hardening

Date: 2026-09-25

Exact base: `main@884fdcc27cb732f461a456ba5d207186ebbbc0be`.

## Convergence

This is the current-main successor to stale PR #403. It preserves the newer canonical provider observation contracts already present in main:
- authenticated `ProviderResponseObservation` scope;
- provider/account/environment bindings on fill evidence;
- exact provider evidence references;
- canonically derived VERIFIED capability snapshots.

The stale lineage is not copied wholesale because doing so would remove those later trust boundaries.

## Invariants added

- Binance USD-M provider symbols must be canonical uppercase both on create-order acknowledgements and authenticated trade observations.
- Every key and value in the supplied symbol-to-instrument map is validated before any fill mapping; unused malformed aliases cannot hide in the map.
- Every client-order identity-map key is a non-negative integer provider order ID and every mapped client ID satisfies the canonical Binance client-ID grammar before any fill mapping.
- Falsey non-mappings (for example `[]`) do not silently collapse to an empty map.
- Existing provider/account/environment/evidence lineage is preserved unchanged.

## Evidence boundary

Focused regressions cover lowercase provider observations, malformed unused instrument aliases, malformed/empty instrument identities, invalid client-map container/key/value identities, duplicate execution identity and exact fee preservation.

Required before merge: `baseline`, `reconvergence-integrity`, and full `Verify AutoTrade` on the exact head.

No provider qualification, credential, send, withdrawal, real-money or profitability claim is introduced.
