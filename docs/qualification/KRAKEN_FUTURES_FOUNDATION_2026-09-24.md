# Kraken Futures adapter foundation evidence — 2026-09-24

Status: **implementation foundation only; NOT QUALIFIED for live or demo
trading authority**.

This lineage deliberately owns only the Futures half of WP-23. Concurrent
Kraken Spot lineages appeared while this work was being prepared; duplicating
Spot would violate the one-authority rule.

## Public semantics checked

Sources checked on 2026-09-24:
- https://docs.kraken.com/api/docs/futures-api/auth/check-v-3-api-key
- https://docs.kraken.com/api-reference/account-history/get-execution-events
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
5. Authenticated account execution history is the candidate direction-bearing
   fill surface for this foundation. Its exact response is bound to the exact
   Kraken Futures provider environment (`LIVE` or `DEMO`), kept separate
   from runtime `LIVE`/`PAPER`. The current official response contract uses
   the case-sensitive `event.Execution` envelope and requires a top-level
   `accountUid`; embedded order `accountUid` must agree with that response
   identity before any fill evidence is emitted. The public schema currently
   documents execution UID, tradeable, client ID, exact execution
   quantity/price/time and `orderData.fee`, but does not by itself qualify an
   order `direction` field. AutoTrade therefore emits `ProviderFillEvidence`
   only when `direction` is actually present in the authenticated exact bytes
   as `Buy` or `Sell`; provider qualification must separately prove that
   field on the advertised environment before this surface is treated as
   direction-complete.
6. Fee currency is not invented from the execution payload. The parser requires
   a separately qualified fee-currency mapping for the exact tradeable before
   it can emit bookable provider fill evidence.
7. Authenticated position history remains useful for position-event
   reconciliation, but it is not used to infer BUY/SELL direction.
8. Duplicate execution identity with conflicting economics fails closed.
9. Empty history does not prove absence until pagination, consistency horizon
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
