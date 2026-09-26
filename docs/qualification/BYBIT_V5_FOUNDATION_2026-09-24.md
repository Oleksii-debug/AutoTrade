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


## Уточнення повноти комісій виконання

REST `/v5/execution/list` не вважається повним економічним доказом лише через наявність `execFee`. Документований linear-приклад Bybit містить ненульовий `execFee` з порожнім `feeCurrency`, тому адаптер не виводить валюту з символу, котирувальної чи розрахункової валюти. За порожнього `feeCurrency` потрібне окреме кваліфіковане зіставлення для точної версії інструмента; без нього перетворення в канонічний `ProviderFillEvidence` завершується fail-closed.

Поле `extraFees` також входить до provider execution economics. Поки для його юрисдикційних складових немає канонічного представлення й кваліфікованої одиниці, будь-яке непорожнє значення блокує створення повного fill evidence. Порожні форми не додають економічного факту. Це навмисно не є твердженням про повну WP-22 qualification.


## Environment-bound response provenance

Recorded order-submission evidence is not environment-neutral. The adapter now requires an explicit `MAINNET`, `TESTNET` or `DEMO` environment for every parsed create-order response and binds the evidence URI to the corresponding documented REST service (`api.bybit.com`, `api-testnet.bybit.com`, or `api-demo.bybit.com`). Unknown environments fail closed. This prevents testnet/demo observations from being mislabeled as mainnet evidence; regional/entity-specific production endpoints remain outside this bounded foundation until separately qualified.

## Order reconciliation page foundation — 2026-09-26

The adapter now has a network-free, capability-bound request/parser seam for
Bybit V5 `/v5/order/realtime` and `/v5/order/history`. The implementation
preserves the provider's opaque `nextPageCursor`, binds every response to the
exact authenticated query evidence, and requires the response `category` and
an exact queried `orderLinkId` to match before order-state evidence is
accepted.

For order history, caller-supplied start/end windows are fail-closed at the
documented maximum of seven days and page limits are bounded to 1..50.
Realtime-order reads reject history-only time-window parameters. Order status is
preserved as provider state and is deliberately **not** promoted to canonical
fill evidence; execution economics still require `/v5/execution/list` and
reconciliation.

An empty final cursor proves only that this exact read finished pagination. It
does not by itself set
`provider_semantics_exclude_execution=true`, does not resolve an UNKNOWN send,
and does not qualify MAINNET/TESTNET/DEMO behavior without recorded exact-build
provider evidence.

Current official references:
- https://bybit-exchange.github.io/docs/v5/order/open-order
- https://bybit-exchange.github.io/docs/v5/order/order-list

## Execution pagination foundation — 2026-09-26

The execution-history seam now prepares bounded `/v5/execution/list` queries,
preserves the opaque provider cursor, validates response category and exact
queried `orderLinkId`, and derives `EXECUTIONS` pagination coverage only
from a contiguous first-page-to-terminal-page chain with an explicit
`startTime`/`endTime` window. The explicit window is limited to the
documented seven-day maximum and each page remains economically fail-closed
through the existing fee-currency and `extraFees` checks.

A complete cursor chain still does not, by itself, assert provider exclusion
semantics. `provider_semantics_exclude_execution` remains false unless exact
provider/product/environment qualification separately establishes that stronger
claim.

## Working-order reconciliation projection — 2026-09-26

The realtime order seam now preserves `leavesQty` as exact decimal state and
projects only Bybit's documented open order statuses — `New`,
`PartiallyFilled`, and `Untriggered` — into the existing canonical
`ProviderWorkingOrderEvidence`. Closed statuses are retained in the provider
page but are not emitted as working orders. Unknown future provider statuses
fail closed instead of being silently treated as open or closed.

The realtime request explicitly sends `openOnly=0`; linear realtime reads
require an uppercase symbol scope in this bounded adapter path. The projection
requires an explicit provider-symbol to canonical instrument-version mapping,
so provider symbols cannot silently become canonical instrument identities.

This creates a direct reconciliation path for an UNKNOWN send: a later exact
realtime read that finds the same `orderLinkId`, combined with the existing
causal `SnapshotConsistencyEvidence`, can resolve the submission as an
observed working order without blind retry. It still does not establish provider
qualification or absence semantics.

## Account snapshot and activity reconciliation foundation — 2026-09-26

The adapter now has exact-byte authenticated read seams for the remaining
account-reconciliation surfaces required by the canonical provider contract:

- `/v5/account/wallet-balance` is bound to `accountType=UNIFIED`, optional
  exact coin scope, the verified account/environment capability, and explicit
  Bybit provider environment. Wallet liabilities (`borrowAmount` and
  `spotBorrow`) are preserved. Generic cash reconciliation is deliberately
  refused whenever either liability is non-zero; borrowed buying power is not
  re-labelled as owned cash.
- `/v5/position/list` preserves category, `positionIdx`, side, positive
  provider size, lifecycle status, update time, sequence, opaque cursor and
  exact response evidence. Complete one-way (`positionIdx=0`) cursor chains
  can project to canonical signed position quantities using an explicit
  provider-symbol to instrument-version mapping. Hedge-mode legs are not netted
  away, and `Liq`/`Adl` states fail closed rather than being treated as a
  READY account snapshot.
- `/v5/account/transaction-log` is bounded to explicit seven-day windows,
  page size 1..50 and opaque cursor continuation. Each row is projected into
  the existing `ProviderActivityEvidence` using the provider transaction id,
  time, currency, exact `change`, order/client/trade identities and canonical
  instrument mapping where a symbol is present. Provider transaction types are
  preserved as data rather than frozen into a stale allowlist.
- Transaction-log origin is intentionally `UNKNOWN`. The adapter does not
  infer that an event was generated by AutoTrade from its shape. Existing
  reconciliation can match a durable local activity id; unmatched
  MANUAL/EXTERNAL/UNKNOWN activity blocks new risk.

These REST components do **not** manufacture
`SnapshotConsistencyEvidence`. A multi-call Bybit account snapshot is only
usable as a coherent canonical snapshot when the existing reconciliation
authority is separately given an atomic or composed snapshot proof. For
COMPOSED mode that proof requires buffered account-stream events, completed
replay and no detected sequence gap. This follows the canonical rule that
several successful REST calls are not, by themselves, evidence of a consistent
account cut.

No Bybit TESTNET, DEMO or MAINNET account is qualified by these fixtures.
Recorded exact-build provider evidence, credential-scope evidence, retention /
history-lag characterization, stream-gap recovery and the remaining WP-22
qualification matrix are still required before the provider can be promoted
from implementation foundation to qualified execution support.

Official contract references consulted for this foundation:
- https://bybit-exchange.github.io/docs/v5/account/wallet-balance
- https://bybit-exchange.github.io/docs/v5/position
- https://bybit-exchange.github.io/docs/v5/account/transaction-log
- https://bybit-exchange.github.io/docs/v5/enum

