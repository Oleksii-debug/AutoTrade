# Alpaca adapter foundation evidence — 2026-09-24

Status: **implementation foundation only; NOT QUALIFIED for paper or live authority**.

The adapter is deliberately network-free and reuses the canonical
`CapabilitySnapshot`. It creates no credential store, dispatcher, retry
authority, ledger, reconciliation engine or provider-specific capability
authority.

## Current official documentation checked

- Create Order: https://docs.alpaca.markets/us/reference/postorder
- Orders/TIF/extended hours: https://docs.alpaca.markets/us/docs/orders-at-alpaca
- Crypto orders: https://docs.alpaca.markets/us/docs/crypto-orders
- Options orders: https://docs.alpaca.markets/us/docs/options-orders
- Options stop/stop-limit update (2026-08-27):
  https://docs.alpaca.markets/us/v1.1/changelog/2026-08-27-options-stop-orders-08e9371
- Options GTC update (2026-08-28):
  https://docs.alpaca.markets/us/v1.1/changelog/2026-08-28-options-gtc-15a3de4
- Order by client ID: https://docs.alpaca.markets/us/reference/getorderbyclientorderid
- Account activities: https://docs.alpaca.markets/us/docs/account-activities
- Paper trading: https://docs.alpaca.markets/us/v1.4.2/docs/paper-trading
- Trading API schema update (2026-06-24):
  https://docs.alpaca.markets/us/v1.1/changelog/2026-06-24-trading-api-00bf221

## Safety/economic decisions

- exact Decimal only; binary floats are rejected;
- equities, crypto and single-leg options have separate order/TIF constraints;
- option quantities are whole contracts and option notional sizing is rejected;
- crypto may use documented qty or notional with its native GTC/IOC TIFs;
- extended hours is limited to equity LIMIT with DAY/GTC;
- a successful order response is ACKNOWLEDGED only, never a fabricated fill;
- FILL activities become canonical fill evidence only after exact order/client
  identity and separate fee amount/currency evidence are bound;
- absence stays INCONCLUSIVE unless all required surfaces/horizon are complete
  and exact qualification has established exclusion semantics;
- paper evidence cannot prove live execution realism.

The exact adapter build still must pass every
`provider_core.REQUIRED_QUALIFICATION_CASES` before any non-live or live
qualification claim. No real provider request is performed by this foundation.
