# Bybit V5 adapter foundation evidence — 2026-09-24

Status: **implementation foundation only; NOT QUALIFIED for live, demo, testnet or paper authority**.

This file records the public provider semantics used by
`mvp/autotrade_mvp/bybit_v5.py`. It is not qualification evidence and does not
replace recorded provider probes, account evidence, secret handling tests,
stream/reconnect tests or exact-head release qualification.

## Public documentation checked

- Integration/authentication guidance:
  https://bybit-exchange.github.io/docs/v5/guide
- Place order:
  https://bybit-exchange.github.io/docs/v5/order/create-order
- Open orders:
  https://bybit-exchange.github.io/docs/v5/order/open-order
- Order history:
  https://bybit-exchange.github.io/docs/v5/order/order-list
- Trade/execution history:
  https://bybit-exchange.github.io/docs/v5/order/execution
- Private execution stream:
  https://bybit-exchange.github.io/docs/v5/websocket/private/execution
- Wallet balance:
  https://bybit-exchange.github.io/docs/v5/account/wallet-balance
- Position list:
  https://bybit-exchange.github.io/docs/v5/position
- Instrument metadata:
  https://bybit-exchange.github.io/docs/v5/market/instrument
- Server time:
  https://bybit-exchange.github.io/docs/v5/market/time
- Error codes:
  https://bybit-exchange.github.io/docs/v5/error
- Rate limits:
  https://bybit-exchange.github.io/docs/v5/rate-limit
- Demo trading:
  https://bybit-exchange.github.io/docs/v5/demo

## Semantics retained by the foundation

1. V5 create-order covers `spot`, `linear`, `inverse` and `option`.
2. `orderLinkId` is bounded to 36 supported characters and is treated as an
   immutable client identity by AutoTrade.
3. Bybit documents successful place-order response as asynchronous
   acknowledgement. AutoTrade therefore maps it to `ACKNOWLEDGED`, never to a
   fill.
4. Spot market BUY quantity can otherwise be interpreted as quote value.
   AutoTrade explicitly sends `marketUnit=baseCoin` for the bounded spot and
   margin market-order path.
5. Authenticated timestamps must satisfy
   `server_time - recv_window <= timestamp < server_time + 1000`.
6. Execution identity is `execId`; one order can have multiple executions.
   Execution rows remain separate economic facts and duplicate IDs may only be
   collapsed when all mapped economic content is identical.
7. Order/open/history/execution APIs are paginated with provider-specific
   windows. One empty endpoint response never proves a possibly-sent order was
   absent.
8. The adapter's absence-coverage helper defaults
   `provider_semantics_exclude_execution=false`. Only separately recorded,
   exact adapter/product/environment qualification may set that stronger fact.
9. Public instrument limits can change. This foundation does not cache or
   hard-code trading filters and does not claim metadata qualification.
10. Demo trading is an isolated Bybit service. Demo/testnet/live evidence is
    not interchangeable.

## Deliberately absent

- no HTTP/WebSocket client;
- no API key or signature storage;
- no credential injection;
- no live or demo request execution;
- no claim of provider qualification;
- no account-capability inference from marketing support;
- no blind retry after an ambiguous write;
- no inference of fill from request acknowledgement.

Before WP-22 can move beyond foundation status, the exact adapter build must
pass all `provider_core.REQUIRED_QUALIFICATION_CASES` with recorded evidence,
including timeout-after-send, reconnect/gap recovery, partial fills,
cancel/fill races, terminal corrections, account-mode changes, manual activity,
snapshot reconciliation and secret redaction.
