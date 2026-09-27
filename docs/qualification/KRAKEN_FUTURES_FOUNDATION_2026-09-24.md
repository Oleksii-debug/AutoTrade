# Kraken Futures adapter foundation evidence — 2026-09-24

Status: **guarded HTTP/signing implementation foundation only; NOT QUALIFIED
for live or demo trading authority**.

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
8. Current Derivatives REST v3 authentication hashes the exact URL-encoded
   parameter representation together with the nonce and signing path
   `/api/v3/sendorder`, then HMAC-SHA512 signs the digest with the
   base64-decoded API secret.
9. State-changing `sendorder` uses POST with the exact URL-encoded order
   parameters carried in the request URL. The bytes used as the `Authent`
   input must therefore be the same encoded query representation transmitted
   on the wire.

## Implemented guarded transport foundation

- Canonical prepared requests project exact account, runtime/provider
  environment, capability, entity, instrument and body digest into the shared
  guarded transport seam.
- LIVE and DEMO HTTPS destinations are explicit provider policies; DEMO maps
  only to PAPER runtime authority.
- TRADE secrets resolve only after quota/current-capability admission, and the
  exact capability is rechecked immediately before the final send guard.
- A journal-backed nonce authority is scoped by provider environment and a
  non-secret API-key fingerprint and stays monotonic across restart/clock
  regression.
- `Authent` is computed from the exact percent-encoded query representation
  transmitted in the POST URL; write requests cannot carry both a query and
  semantic body.
- Exactly one wire send occurs after the final guard. There is no transport
  retry; post-send ambiguity remains the canonical `UNKNOWN` /
  reconciliation-first path.

## Deliberately absent

- no concrete private WebSocket transport;
- no live/demo calls or credential attachment in this evidence;
- no qualification claim for provider permissions, quota behavior,
  timeout-after-send, reconnect/gap recovery, pagination, manual activity or
  account-mode changes;
- no claim that Futures demo evidence qualifies Spot;
- no trading authority.

Before the Futures half of WP-23 is qualified, the exact adapter must pass the
canonical provider suite with recorded evidence for auth/permissions, quota
tiers, timeout-after-send, reconnect/gap recovery, partial fills, cancel/fill
races, history pagination, manual activity, account-mode changes,
reconciliation and secret redaction.
