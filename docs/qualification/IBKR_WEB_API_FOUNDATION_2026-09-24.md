# IBKR Web API foundation qualification boundary — 2026-09-24

## Exact scope

This evidence note applies to the non-network foundation in:

- `mvp/autotrade_mvp/ibkr_web.py`
- `mvp/tests/test_ibkr_web_adapter.py`

It is not an IBKR paper/live qualification and does not grant trading authority.

## Provider contract facts represented

The foundation keeps Web API brokerage-session readiness explicit, binds orders to
the exact account plus `conid` or routed `conidex`, preserves AutoTrade
financial numbers as exact Decimal text until a separately qualified provider
serialization boundary, and treats provider acknowledgement as distinct from
economic fill evidence.

IBKR can return an order reply message instead of an acknowledgement. Such a
reply requires a second `/iserver/reply/{replyId}` request before the order can
be put to work. The adapter therefore represents this as `REPLY_REQUIRED`.
It never auto-confirms the reply and never enables session-wide warning
suppression. A confirmation request is merely prepared after explicit
authorization and must still cross AutoTrade's normal durable guarded send
barrier.

Unique execution identity is carried into the canonical
`ProviderFillEvidence`. The provider `permId` must be a positive integer and
an execution is rejected if its account differs from the reconciliation account,
so evidence cannot drift across accounts. For the TWS-shaped execution helper, fee amount, fee currency and trade time
must be supplied from separately observed evidence. The Web API trades parser
instead consumes the documented execution id, cOID/order reference, conid,
quantity, price, commission and trade time directly; because the trades row does
not establish a canonical commission currency, that currency remains separately
bound evidence. Neither path manufactures economics from an order acknowledgement
or order-status summary.

## Current official references

- https://www.interactivebrokers.com/docs/web-api/trading/trading-sessions-in-the-web-api
- https://www.interactivebrokers.com/docs/web-api/v1/endpoints/orders/place-order
- https://www.interactivebrokers.com/docs/web-api/trading/orders/order-reply-messages
- https://www.interactivebrokers.com/docs/web-api/v1/endpoints/order-monitoring/live-orders
- https://www.interactivebrokers.com/docs/web-api/v1/endpoints/order-monitoring/trades
- https://www.interactivebrokers.com/docs/tws-api/ref/execution

## What remains unqualified

WP-26 remains incomplete. Required future evidence includes exact API/SDK
revision, authenticated paper-session lifecycle, provider numeric serialization,
contract rules and market-data entitlement behavior, rate/session expiry,
stream/order-history reconciliation, manual activity, partial fills/corrections,
US futures manualIndicator rules, reconnects, crash/restart ambiguity, and a
recorded paper/provider test matrix.

No claim is made that paper behavior proves live execution realism, that every
IBKR asset class shares identical semantics, or that a provider reply warning is
safe to suppress.

## Distribution gate update — 2026-09-25

The official IBKR TWS API changelog states that TWS API 10.49+ is released under the GNU GPL as of 2026-08-03. This foundation therefore remains on the official Web API route and does not import or redistribute the TWS SDK. Any future TWS route requires an explicit exact-composition distribution decision; an Apache-licensed wrapper alone is not evidence that the combined distribution is acceptable.
