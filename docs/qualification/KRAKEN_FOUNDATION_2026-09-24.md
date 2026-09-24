# Kraken Spot/Futures adapter foundation evidence — 2026-09-24

Status: **implementation foundation only; NOT QUALIFIED for live or demo
trading authority**.

The module `mvp/autotrade_mvp/kraken.py` is a pure contract adapter. It does
not perform network I/O, load API secrets or grant execution authority.

## Public semantics checked

- Kraken Spot REST trading API: `POST /0/private/AddOrder`.
- Spot private requests use a monotonically increasing `nonce` and
  `API-Key` / `API-Sign` authentication.
- Kraken client identifiers support UUID forms and free ASCII text up to
  18 characters; `cl_ord_id` is unique across open orders for the client.
- Kraken Futures uses a distinct service and request family, including
  `/derivatives/api/v3/sendorder`.
- Futures `sendorder` uses `orderType`, `symbol`, `side`, `size`,
  optional `limitPrice`, `cliOrdId` and `reduceOnly`.
- Futures has a distinct demo base URL
  `https://demo-futures.kraken.com`; this is not evidence for a Spot sandbox.
- Futures authenticated history exposes position events with
  `executionUid`, `executionPrice`, `executionSize`, fees, fill time and
  update reason; pagination/history coverage must be treated explicitly.

Primary sources checked on 2026-09-24:
- https://docs.kraken.com/api/docs/rest-api/get-api-key-info
- https://docs.kraken.com/api/blog/cl-ord-id/
- https://www.kraken.com/features/trading-api
- https://docs.kraken.com/api/docs/futures-api/auth/check-v-3-api-key
- https://docs.kraken.com/api/docs/futures-api/history/get-position-events
- https://docs.kraken.com/api/docs/futures-api/trading/cancel-all-orders-after
- Kraken's official `krakenfx/kraken-cli` default branch, specifically its
  Spot `AddOrder` and Futures `sendorder` request/response mappings.

## Safety retained by the foundation

1. Spot and Futures environment/endpoint identities are never collapsed.
2. A successful provider response maps only to `ACKNOWLEDGED`, never fill.
3. An ambiguous transport maps to `UNKNOWN` and `RECONCILE_FIRST`; there is
   no blind economic retry.
4. Spot nonce validation is strictly monotonic for a caller-owned API-key
   sequence. The adapter itself does not invent or persist nonces.
5. Decimal size/price/fee inputs reject binary floats.
6. Spot trade IDs and Futures `executionUid` remain economic identities.
7. Duplicate Futures execution IDs with conflicting economics fail closed.
8. Reconciliation absence surfaces default to
   `provider_semantics_exclude_execution=false`. Empty provider history is
   not proof that an ambiguously submitted order was never executed.
9. Spot trade fee currency must be supplied by an explicit qualified
   pair/account mapping; it is not guessed from a symbol string.
10. No public Spot sandbox is assumed or advertised.

## Deliberately absent

- no HTTP/WebSocket transport;
- no API secret handling or signature generation;
- no rate-limit scheduler or hidden retry;
- no live/demo request execution;
- no account entitlement inference;
- no provider qualification claim.

WP-23 can advance from foundation status only after an exact adapter build
passes the canonical provider qualification suite with recorded Spot and
Futures evidence: nonce collision/restart behavior, quotas, timeout-after-send,
reconnect/gap recovery, partial fills, cancel/fill races, history pagination,
manual activity, account-mode changes, reconciliation and secret redaction.
