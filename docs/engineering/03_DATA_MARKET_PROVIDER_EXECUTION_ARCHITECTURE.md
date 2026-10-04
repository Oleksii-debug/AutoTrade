# AutoTrade — data, providers and execution

Baseline 2026-09-22. Contracts: document 02. Trading is API-only. Registration, identity verification and provider administration are outside the runtime. All six providers remain architectural targets; country assumptions do not determine selection.

## 1. Data pipeline and ownership

Each adapter has separate public-data, authenticated-read and trading capabilities. Market subscriptions are shared within a host to reduce duplicate fees and quota consumption. Raw evidence is written before normalized publication when retention is permitted. Every normalized observation carries the raw reference, adapter version, instrument version and availability time.

The data coordinator owns sequence validation, freshness and subscription state; adapters own provider-specific parsing and reconnect rules. Analytical transforms never mutate raw records. A provider correction produces a new revision and an invalidation event for affected derived features/experiments. Frozen experiments remain reproducible on their original manifests and are labelled superseded if their data was materially wrong.

Book startup is snapshot → buffered deltas whose sequence follows the snapshot → checksum/continuity check → executable state. A gap, cross, invalid quantity, impossible timestamp or checksum failure marks the book unusable, resubscribes and rebuilds. Trading must not silently use the last apparently plausible book. Quotes, mark prices, index prices and last trades have distinct meanings; risk chooses an explicit conservative valuation hierarchy.

Historical import partitions by source/instrument/date/schema, retaining original resolution, coverage and rights. Missing ticks cannot be reconstructed from bars. A bar feed supports bar-based strategies and conservative fill assumptions, not claims about queue position or intrabar path. Maintain both raw and adjusted equity prices with corporate-action factors; execution uses contemporaneous tradable prices. Point-in-time universes include delisted assets. Macro and news retain vintages and first availability. Calendar/session/DST and contract-roll maps are versioned inputs.

## 2. Official API research matrix

The table distinguishes documented product surfaces from capabilities to be discovered on an actual account. It does not promise that any specific account can trade every listed asset. No provider is removed based on the owner's location.

| Provider | Data/account/execution surfaces | Assets and order semantics to qualify | Test and quota plan |
|---|---|---|---|
| Bybit | V5 REST metadata/history/accounts/orders; public/private WebSocket streams | Spot, linear/inverse derivatives and options categories; mode-specific leverage and position side; reduce-only and conditional orders | Separate demo/test/live configuration. Discover category/account limits; handle HTTP and stream quotas independently. Confirm exact sandbox feature parity. |
| Kraken | Separate Spot and Derivatives REST/WS APIs; derivatives additionally exposes FIX | Spot/margin and derivatives are separate adapters under one provider family; stop, take-profit and trailing behavior requires endpoint-specific mapping | Derivatives demo is documented. Do not assume an equivalent public spot sandbox. Tier/counter-based quotas, persisted nonce where required. |
| WhiteBIT | REST market/history/account/order endpoints; public/private streams; collateral product endpoints | Spot and collateral margin/futures; limit/market/stop/conditional/reduce-only rules vary by endpoint/account surface | A complete production-equivalent sandbox was not established in this research. Qualify recorded fixtures, local simulator and any officially provided test facility before authorized live probes. Quotas are endpoint-specific. |
| Binance | Separate Spot, Margin, USD-M, COIN-M and Options API families; REST and streams | Spot trading is not interchangeable with margin/futures. Filters, price/quantity/notional rules, position mode, contract payoff and reduce-only are product-specific | Spot testnet and derivative test facilities are separately configured and qualified. Read advertised request-weight/order-count limits, response headers and ban/backoff semantics. |
| Interactive Brokers | TWS/IB Gateway socket API; official Web API alternative; contracts, historical/live market data, account/order/execution/activity surfaces | Equities, futures, options, FX and other entitled contracts; contract IDs, combo legs, exercise/assignment, sessions and margin are essential | Paper account, subscribed data and session lifecycle required. Pacing is account/subscription dependent; do not hard-code old universal rates. SDK packaging gate in document 01. |
| Alpaca | Trading REST/stream, assets, historical/live market data, account activities | Equities, crypto and options under their respective rules; account trading levels, fractional quantities, bracket/OCO and multi-leg limitations | Separate paper credentials and endpoints. Paper fill realism is limited; subscription/product quotas and permissions must be discovered and tested. |

Official source register:

- Bybit: [create order](https://bybit-exchange.github.io/docs/v5/order/create-order), [private order stream](https://bybit-exchange.github.io/docs/v5/websocket/private/order), [recent orders](https://bybit-exchange.github.io/docs/v5/order/open-order), [history](https://bybit-exchange.github.io/docs/v5/order/order-list).
- Kraken: [API overview](https://docs.kraken.com/exchange/guides/overview), [add order](https://docs.kraken.com/api-reference/trading/add-order), [REST quotas](https://docs.kraken.com/exchange/guides/rest/ratelimits), [derivatives and demo](https://docs.kraken.com/exchange/guides/futures/introduction).
- WhiteBIT: [API overview](https://docs.whitebit.com/api-reference/overview), [markets](https://docs.whitebit.com/concepts/markets), [order types](https://docs.whitebit.com/concepts/order-types), [futures](https://docs.whitebit.com/products/futures/overview), [account streams](https://docs.whitebit.com/websocket/account-streams/overview), [changelog](https://docs.whitebit.com/changelog).
- Binance: [official documentation](https://developers.binance.com/en/docs), [Spot REST](https://developers.binance.com/en/docs/products/spot/rest-api), [WebSocket API](https://developers.binance.com/en/docs/products/spot/web-socket-api), [maintained Spot specification](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md).
- IBKR: [TWS API](https://www.interactivebrokers.com/docs/tws-api/doc/introduction), [changelog](https://www.interactivebrokers.com/docs/tws-api/changelog).
- Alpaca: [orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca), [paper](https://docs.alpaca.markets/us/docs/paper-trading), [options](https://docs.alpaca.markets/us/docs/options-trading).

## 3. Concrete adapter consequences

Bybit request acknowledgement is asynchronous; private executions/order reports and reconciliation determine outcome. Duplicate Filled notifications can occur during a cancel/fill race. The adapter must distinguish execution IDs from order-status messages. Recent-order retention is bounded and history can lag, so an empty page is inconclusive absence evidence.

Kraken Spot and Derivatives have distinct authentication, symbols, quota and product semantics. The documented derivatives demo has a different base URL. Use a per-credential nonce allocator when required; multiple workers cannot independently invent nonces for one key. Withdrawal permissions are outside AutoTrade's trading interface even if the provider API supports them.

WhiteBIT market slippage protection can leave an unfilled remainder cancelled. It is a partial execution with a terminal remainder, not a failed whole order. Collateral reduce-only may resize to the position, and conditional orders have separate restrictions. Map each actual account's supported order types from documented/API evidence; do not infer product scope from a user's country. A newer streaming API in a changelog is an upgrade candidate, not permission to switch an already qualified adapter silently.

Binance `exchangeInfo` supplies symbol filters and quota information. Normalize product-specific market-buy quantity/notional rules and separate spot versus derivatives client-ID/query semantics. Preserve exchange filter version used for admission and record a rejected rule change. An adapter refreshes metadata before another attempt; it never retries the original economic action blindly after an ambiguous write.

IBKR requires stable contract identity rather than ticker-only routing. Account and execution reconciliation must include manual activity and lifecycle events. The August 2026 SDK license change is a distribution gate described in document 01. Preserve external order identifiers across sessions and test reconnect/client-ID ownership; no claim that a generic HTTP reconnect reconstructs a brokerage session.

Alpaca options use contract metadata and approved trading levels; whole-contract quantity and supported order attributes differ from equities. Assignments are not delivered through the order WebSocket and require activity polling. Paper non-trade activities can be delayed until the next day. Therefore a quiet stream cannot prove no assignment occurred. Paper trading also omits important live execution effects, so it cannot independently prove profitability.

## 4. Rate limits and connectivity

An adapter declares quota buckets by endpoint group, account/key/IP scope, cost weights and observed reset basis. Reserve quota for cancel/protection/reconciliation. Research backfills cannot starve financial recovery. Avoid hard-coded universal numeric rate limits; initial policy loads current documented limits and conservatively updates from provider feedback. Retry-After and ban state are respected. A provider outage opens a circuit for new exposure but not a fake “all orders cancelled” result.

Read retries use bounded exponential backoff with jitter and a total deadline. Write retry behavior depends on provider idempotency guarantees and reconciliation evidence. The host clock is monitored against provider/server time; authentication clock drift stops sends when outside allowed bounds. Monotonic time governs deadlines, UTC records timestamps. Credential refresh never changes the account or environment implicitly.

## 5. Order state machine

There are two related state machines; combining them loses ambiguity.

Intent admission: DRAFT → RISK_REVIEW → AWAITING_CONFIRMATION or AUTHORIZED → RESERVED → DISPATCH_READY. Rejection/expiry/revocation before SEND_STARTED produces NOT_SENT with released reservation. An intent version is immutable after admission.

External order: SEND_STARTED → ACKNOWLEDGED or REJECTED or UNKNOWN. ACKNOWLEDGED → WORKING → PARTIALLY_FILLED → FILLED / CANCELLED / EXPIRED. A fill can arrive before acknowledgement or after a cancellation request. CANCEL_REQUESTED and REPLACE_REQUESTED are pending actions, not terminal order states. UNKNOWN can resolve into any evidenced state. Economic corrections may arrive after a terminal operational status and must still change the ledger.

Invariant: filled quantity is derived from unique executions plus corrections. A terminal cancellation applies to remaining quantity, not already filled quantity. Replace can mean atomic amendment or two linked orders; the adapter declares which. Reserve maximum plausible exposure across old/new orders until external facts exclude overlap. OCO/brackets are not assumed atomic: both legs may execute during races, and child activation can depend on parent partial/full fill semantics.

Protection is a first-class policy. Prefer qualified native orders where appropriate, but document trigger source, gap behavior, exchange persistence, session expiry and cancellation dependencies. Synthetic protection depends on host/data/API availability. The UI explains this distinction in plain text.

## 6. Reconciliation and restart

At startup enter RECOVERING with new exposure blocked. Verify journal integrity and schema; rebuild projections if necessary; acquire exclusive host authority; establish authenticated provider sessions; snapshot balances, positions and all working orders; fetch executions/activities with overlap from the last durable watermark; deduplicate; detect corrections; reconcile unknown submissions and external/manual orders; assess protection and margin; only then enter READY under valid authority.

Where a provider cannot offer an atomic account snapshot, record query start/end and stream watermarks, buffer events during snapshot, replay after the snapshot cut and repeat on inconsistent deltas. Never claim a consistent snapshot solely because several REST calls succeeded.

Every UNKNOWN submission keeps worst-case remaining risk. Query by stable client ID and provider ID, then inspect open, historical, execution and activity surfaces over their documented consistency windows. PROVEN_ABSENT requires complete relevant coverage and provider semantics adequate to exclude execution. Otherwise keep INCONCLUSIVE and block conflicting new risk. A replacement economic intent needs a new admission; it is not a retry flag on the old packet.

Manual orders are imported as externally originated events and consume exposure. Policy can pause new activity or permit coordination, but cannot ignore them. Unexplained cash/position differences create an exception with evidence and block risk increases for the affected scope. Fee/financing lags use provisional accruals reconciled to later actual charges, clearly distinguished in reports.

## 7. Adapter qualification deliverable

Each provider work package delivers: exact API/docs version; endpoint/environment matrix; permitted credential scopes; feature/capability matrix; symbol and instrument fixtures; authentication/nonce/clock tests; quota tests; stream gaps/reconnect fixtures; full lifecycle/correction fixtures; order/idempotency ambiguity tests; account snapshot reconciliation; test-environment evidence; cost/rate assumptions; secret-redacted logs; unsupported-feature reasons; and a signed capability qualification report tied to code SHA.

Recorded fixtures and official sandbox tests precede any separately authorized real-account probe. Tests must cover market and limit, partial fills, cancel/fill race, amendment, rejection, timeout-after-send, duplicate/lost events, terminal corrections, account mode change, protection and manual activity. Asset-specific futures/options/funding fixtures are required only when advertising those capabilities, but the complete final product must eventually qualify its approved scope.

## 8. Performance envelope

AutoTrade is not an exchange-colocated ultra-low-latency promise. Benchmark ingest, normalization, risk admission and dispatcher latency separately from network/provider time. The selected strategy's horizon and measured edge must tolerate the measured latency distribution. Sustained backlog, dropped sequences or disk latency degrades data quality and blocks affected new risk. Load tests use at least the declared deployment workload plus a documented burst margin; publish actual numbers rather than claiming universal throughput from framework marketing.
