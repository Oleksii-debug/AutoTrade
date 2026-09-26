# Binance USD-M exchangeInfo admission hardening — 2026-09-26

Status: **source implementation only; NOT QUALIFIED for testnet, live, or real-money authority**.

Lineage:
- WP-25 canonical Binance foundation;
- stacked after the reconciliation/identity hardening in PR #915;
- implementation review surface: PR #924.

This evidence note records what the adapter now enforces and, equally
importantly, what it still does not prove. It must not be used as a provider
qualification artifact.

## Implemented admission boundaries

USD-M order preparation now requires a version-bound
`/fapi/v1/exchangeInfo` response issued by the shared provider transport.
The transport accepts only the explicitly allowlisted Binance USD-M origin,
endpoint and canonical query for the exact runtime environment, revalidates the
VERIFIED capability, performs one GET, preserves the HTTP status and exact
response bytes, parses the exact response inside that composition boundary, and
passes the resulting rules directly into deterministic order validation. The
public order-preparation API does not accept caller-supplied rule or mark-price
objects. The internal rules retain provider, environment, endpoint/query,
instrument version, response SHA-256, evidence identity, origin, and exact
request-URL digest.

The adapter validates the following provider rules before a LIMIT or MARKET
request can be prepared:

- canonical symbol identity and exact AutoTrade instrument version;
- symbol `status=TRADING`;
- provider-advertised `orderTypes` and `timeInForce`;
- `PRICE_FILTER` min/max/tick rules;
- `LOT_SIZE` min/max/step rules;
- `MARKET_LOT_SIZE` min/max/step rules when present;
- `PERCENT_PRICE` BUY cap / SELL floor for LIMIT orders when present;
- `MIN_NOTIONAL` when present, with Binance's reduce-only exemption.

Filter increments use Binance USD-M's documented offset semantics:
`(price - minPrice) % tickSize == 0` and
`(quantity - minQty) % stepSize == 0`. The adapter deliberately does not use
`pricePrecision` as tick size or `quantityPrecision` as step size.

Where a rule depends on mark price, the same shared composition fetches and
parses `/fapi/v1/premiumIndex?symbol=<exact symbol>` itself before deterministic
validation. A locally fabricated `ProviderResponseObservation`, rule object, or
mark-price object cannot be supplied to the public order-preparation entry
point. The internal evidence is bound to
provider/environment/origin/endpoint/query/instrument/symbol, exact request URL
digest and exact response digest; the provider timestamp cannot be after the
network observation, cannot be from the future at admission, and must satisfy
an explicit maximum age.

For MARKET `MIN_NOTIONAL`, mark price is used because USD-M documents that
MARKET orders have no order price and use mark price for the notional rule.
For LIMIT `PERCENT_PRICE`, BUY is checked against
`markPrice * multiplierUp` and SELL against
`markPrice * multiplierDown`.

## Deliberately not claimed

This source hardening does **not** qualify or implement the remaining WP-25
provider authority. In particular it does not prove:

- USD-M authenticated HTTP signing, credential permissions, server-time skew
  control, `recvWindow`, or write retry behavior; the public admission GET
  path added here carries no credential or signing authority;
- USD-M authenticated-read transport or user-data-stream lifecycle/recovery;
- exact open-order count admission for `MAX_NUM_ORDERS`;
- a predictive MARKET `PERCENT_PRICE` pass: Binance can reject a MARKET order
  when the counterparty best price is outside the filter, which cannot be
  guaranteed from static exchangeInfo + prior mark-price evidence;
- account/margin/liquidation/position snapshot completeness;
- funding, realized-PnL, correction, manual/external activity, or complete
  pagination/retention semantics;
- live/testnet behavior, profitability, or real-money safety.

Provider rejection remains distinct from execution evidence. ACK is not a fill,
and timeout/UNKNOWN must still reconcile through the canonical execution
authority.

The source policy currently maps LIVE USD-M REST traffic to
`https://fapi.binance.com` and PAPER/demo traffic to
`https://demo-fapi.binance.com`. LIVE `fapi.binance.com` is present in the
current official USD-M API reference. PAPER/demo remains subject to the
provider-qualification gate and must not be treated as empirically qualified
until the exact delivered build completes the external provider campaign.

## Official semantics checked

Checked 2026-09-26 against Binance Futures (USDⓈ-M) documentation:

- Public Endpoints Info / symbol filters:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/common-definition
- Exchange Information:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information
- Error Codes, including UNKNOWN/TIMEOUT, filter errors, MARKET percent-price
  rejection, and the reduce-only MIN_NOTIONAL exception:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/error-code

## Merge evidence required

Before this work can be integrated, the exact repaired PR head still requires
successful `baseline`, `reconvergence-integrity`, and full
`Verify AutoTrade`. The provenance regression suite must also prove that raw
caller mappings, locally fabricated exact observations/rules/mark prices,
arbitrary production wire-client injection, wrong
provider/environment/origin/endpoint/query/instrument scope, non-success HTTP
responses and changed exact response bytes cannot silently authorize
ORDER_WRITE admission.
Those checks are implementation evidence only; they are not provider
qualification.
