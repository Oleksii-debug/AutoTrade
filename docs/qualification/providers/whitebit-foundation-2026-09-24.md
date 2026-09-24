# WhiteBIT adapter foundation evidence — 2026-09-24

Status: IMPLEMENTATION_FOUNDATION_ONLY — NOT PROVIDER_QUALIFIED.

## Exact source state

- AutoTrade base SHA: `6c943052c42f299b8535cad43c9e6216b33da674`.
- Work package: WP-24 / PROVIDER / whitebit-adapter.
- Adapter transport: none. The foundation does not authenticate, open sockets, submit orders, cancel orders, or perform retries.
- External runtime dependency: none added.
- Candidate SDK remains provenance-only: WhiteBit.Net revision `5ef49daa517abe86a8e35a85256db184cffdfdb2`; no SDK byte is imported by this change.

## Official documentation observed

- https://docs.whitebit.com/api-reference/overview
- https://docs.whitebit.com/concepts/order-types
- https://docs.whitebit.com/guides/client-order-id
- https://docs.whitebit.com/api-reference/trading/create-limit-order
- https://docs.whitebit.com/api-reference/trading/query-executed-orders
- https://docs.whitebit.com/products/futures/overview

The implementation deliberately uses only semantics required by the canonical AutoTrade architecture and evidenced in the sources above:

- private writes remain outside this adapter and require signed authenticated transport owned elsewhere;
- exact `clientOrderId` is preserved for reconciliation;
- provider market metadata supplies step/tick/min/max admission fields;
- account/API-discovered capability evidence controls which order types are allowed; no product access is inferred from country;
- `CANCELED_TAKER_BAND` is represented as terminal cancellation of the remainder while preserving observed partial quantity;
- reduce-only clipping cannot flip or enlarge a position;
- order-level aggregate executed quantity is not treated as a unique economic fill;
- exact-client lookup and paged history are distinct query modes;
- a full history page does not prove pagination completion;
- write timeout after the send boundary remains UNKNOWN and requires reconciliation.

## Tests added

`mvp/tests/test_whitebit_adapter.py` covers:

- exact decimal request construction and provider step/tick rejection;
- account-discovered capability restrictions;
- reduce-only clipping and wrong-side rejection;
- market/limit/stop endpoint routing;
- incompatible execution flags;
- strict client-order identifiers;
- partial taker-band cancellation semantics;
- acknowledgement versus fill and UNKNOWN timeout classification;
- no blind retry after rejection/ambiguity;
- exact-client lookup shape;
- contiguous history pagination and final-page proof;
- 31-day history request window and max page limit;
- market-rule parsing;
- nested authentication/debug redaction.

## Qualification still required

This evidence does NOT establish production, paper, sandbox, or real-account qualification. WP-24 remains incomplete until an exact adapter build has recorded-fixture and approved test-environment evidence for authentication/nonce/clock, quotas, streams/reconnect/gaps, unique execution/deal identities, corrections, account snapshots, working/manual orders, collateral/futures lifecycle, conditional orders, fees/funding, endpoint-specific account capabilities, history retention/consistency windows, secret-redacted diagnostics, restart/reconciliation, and the exact dependency/SBOM graph if WhiteBit.Net is adopted.

No live credential, real-money action, withdrawal authority, or live probe is part of this artifact.
