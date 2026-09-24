# Kraken Spot adapter foundation evidence — 2026-09-24

Status: **implementation foundation only; NOT QUALIFIED for live trading, Spot
sandbox/paper authority, or Kraken Derivatives authority**.

This evidence accompanies `mvp/autotrade_mvp/kraken_spot.py`. The module is a
pure translation/recorded-response layer. It does not perform HTTP requests,
store API keys, sign requests, or authorize financial actions.

## Canonical provider facts used

The repository's reviewed provider architecture records these official Kraken
sources and constraints:

- API overview: https://docs.kraken.com/exchange/guides/overview
- Spot Add Order: https://docs.kraken.com/api-reference/trading/add-order
- Spot REST rate limits: https://docs.kraken.com/exchange/guides/rest/ratelimits
- Derivatives/Futures introduction and demo:
  https://docs.kraken.com/exchange/guides/futures/introduction

The architecture explicitly requires Spot and Derivatives to remain separate
API families for authentication, symbols, quotas and product semantics. The
documented Derivatives demo is not treated as proof of equivalent Spot sandbox
parity.

## Semantics enforced by this foundation

1. A successful AddOrder response containing a provider transaction id is
   `ACKNOWLEDGED`, never a fill.
2. Transport failure after the irreversible send cut becomes `UNKNOWN` with
   `RECONCILE_FIRST`; no blind retry is authorized.
3. Client identity is derived deterministically from the canonical AutoTrade
   UUID into an 18-character provider token. The canonical UUID and provider
   token must both remain journaled, and publication must still detect any
   collision.
4. Spot REST nonces are strictly increasing. The helper computes the next
   value, but the caller must reserve/persist it atomically by credential
   identity before signing. Independent in-memory worker counters are not a
   valid nonce authority.
5. Only MARKET and LIMIT are admitted by this bounded foundation. Advanced
   stop, take-profit, trailing and conditional semantics remain unavailable
   until separately mapped and qualified.
6. Monetary/quantity inputs reject binary floating point. Recorded trade
   timestamps are accepted only at exact microsecond-or-coarser decimal
   precision.
7. TradesHistory execution ids are the economic fill identities. Instrument
   version, canonical client identity and fee currency are supplied from
   separately evidenced metadata/order state rather than guessed from a pair
   string.
8. Reconciliation coverage defaults to
   `provider_semantics_exclude_execution=false`. Empty OpenOrders,
   ClosedOrders, TradesHistory or ledger surfaces do not independently prove
   that a possibly-sent order never executed.
9. No withdrawal or transfer method exists in the adapter surface.

## Deliberately unresolved qualification work

WP-23 is not complete. Exact adapter/build qualification still requires recorded
evidence for the canonical provider test bank: authentication/signature and
durable nonce allocation; current metadata and account capabilities; quotas;
stream gaps/reconnect; market and limit orders; partial fills; cancel/fill
races; explicit rejection; timeout-after-send; duplicate/lost events; terminal
corrections; account-mode changes; manual activity; consistent account
snapshot/reconciliation; and secret-redacted logs.

The complete final product must also qualify Kraken Derivatives separately,
including its demo environment, derivative instrument identity, margin,
funding/settlement and lifecycle semantics. No result in this foundation is
evidence of profitability or permission for real-money trading.
