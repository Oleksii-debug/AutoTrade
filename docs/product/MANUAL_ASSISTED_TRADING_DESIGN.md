# Manual and assisted WhiteBIT trading decision

Status: implemented manual ingress and UI; live submission intentionally blocked
until the external activation items below are complete. This is a real WhiteBIT
ticket, not a paper/simulation substitute. No live order was placed while
implementing or testing it.

## Public design references

- [Accessible Trade Terminal overview](https://accessibletrader.com/) describes
  a keyboard-first, screen-reader-native terminal, unified trading dashboard,
  alerts and an analyst that cannot touch the account.
- [Accessible Trade Terminal manual](https://accessibletrader.com/manual)
  documents `Alt+T` for the dashboard with ticket, Positions, Balances, Orders,
  History and Book.
- [Accessible Trade Terminal features](https://accessibletrader.com/features)
  documents opt-in event delivery and plain-language analysis.

Only these public interaction patterns informed this independent AutoTrade
implementation; no third-party source code was copied.

## Authority design

1. `Alt+T` focuses one semantic **Manual trading and assistant** section.
2. The operator selects a canonical enabled WhiteBIT SPOT market (the first UI
   set is `BTC_USDT`, `ETH_USDT`, `SOL_USDT`). The same current public metadata
   response supplies base/quote assets, minimum quantity/notional and precision;
   the same selected market is pinned in the durable draft and reaches the OMS
   unchanged. Public top-of-book remains available without order authority;
   five-second price narration is opt-in.
3. Manual spot `MARKET`/`LIMIT` `BUY`/`SELL` and `CANCEL` first create a
   durable, expiring review draft. No provider call occurs during preparation.
4. A distinct user checkbox and confirmation request with the exact phrase
   `CONFIRM REAL WHITEBIT ORDER` are required before submission.
5. Confirmation rechecks current capability. Only a least-privilege `TRADE`
   credential, exact `ORDER_MANAGEMENT` plus internal `ORDER_WRITE` capability,
   no forbidden permissions, and an injected canonical OMS gateway enable it.
6. A final injected submission guard runs immediately before the gateway.
   Production binds it to the durable runtime-mode authority, so `PAUSED`,
   `RESEARCH_ONLY`, `RECOVERY` and `STOPPED` cannot send even when credentials
   and capabilities are otherwise valid.
7. The gateway is not a second sender. Production composition must translate the
   immutable command into the existing `WhiteBitOrderIntent` /
   `prepare_order_request` / `GuardedDispatcher` / `WhiteBitHttpTransport` path.
8. WhiteBIT cancellation is a canonical guarded transport operation at
   `/api/v4/order/cancel`: exact market plus exactly one `orderId` or
   `clientOrderId`, cancel-reserved quota, final guard, one send, and ambiguous
   outcome recorded as `UNKNOWN` for reconciliation without retry.
9. The assistant may read prices and prepare the same draft. It has no confirm
   method and never receives execution authority.
10. Deposit, withdrawal, balance-transfer and trade-balance operations are absent
   from the request schema and action allowlist. Tests prove they are rejected.

## Current external activation blocker

Credential Manager and the Drive registry contain only READ profile
`whitebit-primary`. A UI-visible `AutoTrade_FullAccess_2026` key includes
`Order_management` but also dangerous Deposit, Withdraw, Balance_transfer and
Trade_balance permissions; its secret is not stored and its IP allowlist is not
verified. AutoTrade therefore cannot and must not use it for this ticket.
Activation requires a separate WhiteBIT credential profile with purpose `TRADE`,
limited to account reads and Order_management with verified IP allowlist, stored
in the protected vault. The protected READ-only `whitebit-primary` profile is
already bound through a five-second cached authenticated main/trading-balance
projection; this does not grant order authority. Production composition must
still issue exact current TRADE capability evidence and inject the canonical OMS
gateway backed by `WhiteBitHttpTransport`. Confirmation remains disabled until
all trading gates pass.

## Included and remaining

Included: live per-market public bid/ask plus current instrument constraints,
accessible real-order ticket, market/limit buy/sell drafts, a canonical guarded
cancel transport, authenticated READ-only account/balance projection, explicit
confirmation,
restart-durable draft/result journal, assistant prepare-only flow, runtime-mode
submission fence, dangerous-action hard denylist and fail-closed tests.

Remaining external integration: create the least-privilege WhiteBIT TRADE key,
and compose the gateway with the canonical live OMS/transport. The existing
`whitebit-primary` profile is already bound only for authenticated account READ
projection and grants no order authority. Stop/trailing/OCO and
collateral/futures are outside this slice.
