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
8. Current Derivatives REST v3 authentication signs
   `SHA256(urlEncodedPostData + Nonce + endpointPath)` with HMAC-SHA512 using
   the base64-decoded API secret. The request URL remains under
   `/derivatives/api/v3/*`, while the signing path is `/api/v3/*`.
9. The request bytes hashed for `Authent` are the same URL-encoded form bytes
   emitted on the wire. This follows Kraken's post-1-October-2025 encoded
   signing behavior rather than the retired pre-encoding algorithm.

## Implemented guarded transport foundation

- `KrakenFuturesPreparedRequest` now projects exact account, runtime/provider
  environment, entity, capability, instrument and canonical body digest into
  the shared guarded transport seam.
- LIVE and DEMO origins are explicit allowlisted HTTPS policies; DEMO maps only
  to the PAPER runtime environment.
- TRADE credentials resolve only after quota and current-capability admission.
  The transport rechecks the exact current VERIFIED capability again after
  signing and immediately before the dispatcher's final guard.
- A journal-backed nonce authority is scoped by provider environment and a
  non-secret SHA-256 fingerprint of the provider API key. It remains strictly
  monotonic across restart and clock regression, including DEMO/PAPER.
- The signer emits the exact URL-encoded body it signs and sends
  `APIKey`/`Nonce`/`Authent` headers. The API secret is not placed in the
  request object, journal, response or returned evidence.
- Exactly one wire POST can occur after the final guard. There is no transport
  retry; post-guard ambiguity remains the existing GuardedDispatcher
  `UNKNOWN` / reconciliation-first path.

## Deliberately absent

- no WebSocket transport;
- no live/demo calls or credential attachment in this evidence;
- no qualification claim for API permissions, provider quota behavior,
  timeout-after-send, reconnect/gap recovery, pagination, manual activity or
  account-mode changes;
- no claim that Futures demo evidence qualifies Spot;
- no trading authority.

Before the Futures half of WP-23 is qualified, the exact adapter must pass the
canonical provider suite with recorded evidence for auth/permissions, quota
tiers, timeout-after-send, reconnect/gap recovery, partial fills, cancel/fill
races, history pagination, manual activity, account-mode changes,
reconciliation and secret redaction.
