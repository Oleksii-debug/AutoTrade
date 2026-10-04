# Kraken Futures adapter foundation evidence — 2026-09-24

Status: **implementation foundation only; NOT QUALIFIED for live or demo
trading authority**.

This lineage deliberately owns only the Futures half of WP-23. Concurrent
Kraken Spot lineages appeared while this work was being prepared; duplicating
Spot would violate the one-authority rule.

## Public semantics checked

Sources checked on 2026-09-24:
- https://docs.kraken.com/api/docs/futures-api/auth/check-v-3-api-key
- https://docs.kraken.com/api/docs/futures-api/history/get-position-events
- https://docs.kraken.com/api/docs/futures-api/trading/cancel-all-orders-after
- official `krakenfx/kraken-cli` default branch, including Futures
  `sendorder` request and `sendStatus` response mappings.

Retained facts:
1. Futures live and demo are distinct services:
   `https://futures.kraken.com` and
   `https://demo-futures.kraken.com`.
2. `sendorder` fields include `orderType`, `symbol`, `side`, `size`,
   optional `limitPrice`, `cliOrdId` and `reduceOnly`.
3. A successful `sendStatus` is only provider acknowledgement. It is not fill
   evidence.
4. Ambiguous transport becomes `UNKNOWN` with `RECONCILE_FIRST`.
5. Authenticated position history can expose trade-caused
   `executionUid`/`executionPrice`/`executionSize`/fee/fill-time facts.
6. Duplicate execution identity with conflicting economics fails closed.
7. Empty history does not prove absence until pagination, consistency horizon
   and exact provider exclusion semantics have separately been qualified.

## Deliberately absent

- no HTTP/WebSocket transport;
- no API key/signature storage or generation;
- no rate-limit scheduler or hidden retry;
- no live/demo calls;
- no claim that Futures demo evidence qualifies Spot;
- no trading authority.

Before the Futures half of WP-23 is qualified, the exact adapter must pass the
canonical provider suite with recorded evidence for auth/permissions, quota
tiers, timeout-after-send, reconnect/gap recovery, partial fills, cancel/fill
races, history pagination, manual activity, account-mode changes,
reconciliation and secret redaction.
