# Binance Spot adapter foundation — 2026-09-24

Status: **implementation foundation only; NOT QUALIFIED for live, testnet, paper or real-money authority**.

This foundation is deliberately network-free. It stores no credentials, creates
no signatures and cannot send an order. It prepares bounded canonical Spot
requests and maps recorded provider responses into existing AutoTrade contracts.

## Public provider semantics retained

The implementation was checked against Binance Developer documentation current
on 2026-09-24, including the Spot product documentation and Spot API glossary.

- Spot `quantity` denotes base-asset quantity. Reverse MARKET
  `quoteOrderQty` has a different economic unit and is deliberately excluded.
- `newOrderRespType=ACK` is requested. An ACK contains order identity and
  transaction time; it is not execution evidence and is never booked as a fill.
- `clientOrderId` / `newClientOrderId` is preserved as provider identity for
  reconciliation.
- Trade rows, not order acknowledgements, become economic fill evidence.
- Exchange filters and permissions are not inferred from marketing support;
  exact capability evidence must admit the order.
- Empty order/history surfaces do not prove a possibly sent order absent unless
  exact qualification has separately established complete coverage and
  exclusion semantics.

References:
- https://developers.binance.com/en/docs/products/spot
- https://developers.binance.com/en/docs/products/spot/faqs/spot_glossary
- https://developers.binance.com/en/docs/products/spot/testnet/TESTNET-TERMS-OF-USE

## Deliberately absent

- HTTP/WebSocket clients;
- API-key/secret handling or request signing;
- live/testnet traffic;
- Margin, USD-M, COIN-M and Options translations;
- exchange-filter ingestion and symbol lifecycle qualification;
- provider absence semantics stronger than recorded exact-build evidence;
- any real-money trading authority.

Before WP-25 may be called qualified, the exact adapter build still needs
recorded authentication/clock/quota/stream/reconnect/order/fill/cancel-race/
timeout-after-send/manual-activity/snapshot-reconciliation/secret-redaction
evidence under `provider_core.REQUIRED_QUALIFICATION_CASES`.
