# WhiteBIT adapter foundation evidence — 2026-09-26

Status: **implementation/recorded-fixture foundation only; NOT QUALIFIED for LIVE
provider authority and NOT evidence of a WhiteBIT paper/testnet environment**.

Exact implementation source revision covered by this evidence:
`c4333e88f07c13ffb9f23bc20702f125d3dafe29`.

Canonical implementation and tests:
- `mvp/autotrade_mvp/whitebit.py`
- `mvp/tests/test_whitebit_adapter.py`

This artifact advances WP-24 without granting financial authority. It records the
provider semantics that can be checked without credentials or network writes and
keeps every missing real-account fact explicit.

## Canonical AutoTrade requirements

`docs/engineering/03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md` requires:
- actual account capability discovery rather than country/product assumptions;
- endpoint/account-scoped quotas with reserved cancel/recovery capacity;
- bounded read retry, but provider/idempotency/reconciliation evidence before a
  possibly-sent financial write can be retried;
- clock-drift fencing;
- acknowledgement distinct from fill;
- reconciliation after UNKNOWN;
- provider qualification tied to the exact source/evidence class.

The canonical provider research also states that a complete
production-equivalent WhiteBIT sandbox was not established. This foundation does
not reinterpret local simulation as provider parity.

## Public provider documentation checked on 2026-09-26

- API overview: https://docs.whitebit.com/api-reference/overview
- Private authentication: https://docs.whitebit.com/api-reference/authentication
- REST rate limits/errors: https://docs.whitebit.com/api-reference/rate-limits
- Security best practices: https://docs.whitebit.com/best-practices/security
- Markets: https://docs.whitebit.com/concepts/markets
- Order types: https://docs.whitebit.com/concepts/order-types
- Spot API: https://docs.whitebit.com/api-reference/spot-trading/overview
- Collateral API: https://docs.whitebit.com/api-reference/collateral-trading/overview
- Futures overview: https://docs.whitebit.com/products/futures/overview
- WebSocket API: https://docs.whitebit.com/websocket/overview
- Changelog: https://docs.whitebit.com/changelog

Observed provider contracts used by the implementation:
1. Private REST requests are HMAC-SHA512 signed and carry a unique incrementing
   nonce. With `nonceWindow`, nonce is Unix milliseconds and must remain within
   the documented ±5 second server-time window.
2. The provider documents per-IP rate limits and warns that endpoint-specific
   limits can override broad defaults. AutoTrade therefore does not embed a
   universal provider quota in its admission policy.
3. HTTP 429 is documented for exponential retry starting at 1 second, doubling
   to a 30-second cap with jitter. Generic 5xx guidance permits backoff, but
   AutoTrade deliberately does **not** blindly retry WRITE/CANCEL after a
   possibly-sent 5xx; those responses enter reconciliation-first handling.
4. WhiteBIT security guidance identifies `Info + Trading` as the minimal
   trading-application permission set and reserves `Deposit + Withdraw` for
   applications that move funds.
5. The same security guidance states that WhiteBIT does not offer a public
   testnet/sandbox and recommends IP whitelisting for production keys.

## Credential boundary

The foundation now has a machine-checked `WhiteBitCredentialBoundary`:
- credential identity is a non-secret binding identifier;
- exact admitted permissions are `INFO` + `TRADING`;
- `DEPOSIT` and `WITHDRAW` are forbidden;
- unknown permission names fail closed;
- actual provider credentials are bound to `LIVE`;
- LIVE admission requires an IP whitelist;
- no API key or secret is stored in this evidence object.

This contract does not prove that any particular account currently has these
permissions. That remains account evidence.

## Nonce ownership and restart contract

`WhiteBitNonceState` and `WhiteBitNonceAllocator` add a provider-local,
secret-free nonce checkpoint:
- nonce scope is a credential-binding identity, not a raw key;
- owner generation fences stale senders;
- a lock prevents same-process concurrent callers from reusing a nonce;
- restored checkpoints continue strictly above the previous nonce;
- nonceWindow drift fails closed without advancing state;
- the checkpoint contains no credential material.

The allocator is **not** a second execution-owner service. Canonical
cross-process sender ownership stays outside this module. The returned checkpoint
must be durably recorded by the active execution owner before a signed request is
submitted. A crash-safe journal/send integration test remains required before
LIVE qualification.

## Rate-limit and retry contract

`WhiteBitRateLimitBudget` takes an evidenced endpoint/window capacity from its
caller. It reserves capacity in this priority order:
1. normal traffic must preserve both recovery and cancel reserves;
2. recovery may use its own reserve but must preserve cancel capacity;
3. cancel has highest admission priority.

`classify_whitebit_http_retry` records:
- 429: bounded exponential backoff with jitter;
- 5xx READ/RECOVERY: bounded backoff;
- 5xx WRITE/CANCEL: no automatic retry; reconciliation required;
- authentication and client/validation failures: no automatic retry.

This is admission/classification logic, not a network scheduler and not proof of
provider quota behavior on an actual account/IP.

## Capability / environment matrix

| Surface | Foundation evidence | Provider qualification |
|---|---|---|
| SPOT | exact decimals, market metadata, market/limit/stop request shapes, order/execution history, spot balances, reconnect policy | **UNQUALIFIED** for actual account/LIVE |
| COLLATERAL | collateral request shapes, reduce-only preservation, positions, collateral balances/borrow, funding, hedge-mode evidence | **UNQUALIFIED** for actual account/LIVE |
| FUTURES | futures market classification, collateral-position/funding primitives, position-side evidence | **UNQUALIFIED** for actual account/LIVE |
| REPLAY | local deterministic evidence only | not a WhiteBIT environment |
| SIMULATION | local deterministic evidence only | not a WhiteBIT environment |
| PAPER | AutoTrade-local evidence class only | WhiteBIT public sandbox/testnet not established |
| LIVE | actual WhiteBIT service | **UNQUALIFIED; no probe authorized here** |

Unsupported/unfinished provider semantics remain fail-closed. In particular this
foundation does not claim universal OCO/OTO/RPI support, account-specific leverage
or position modes, live stream completeness, provider-side idempotency, or
production execution parity.

## Network-free regression evidence expected

Focused command:
`python -m unittest mvp.tests.test_whitebit_adapter -v`

Repository gate:
`python tools/verify.py`

The focused suite now includes:
- exact request/signature/nonceWindow fixtures;
- concurrent nonce uniqueness and restart continuity;
- stale owner-generation rejection;
- credential permission and environment fencing;
- rate-reserve starvation prevention;
- 429 backoff and reconciliation-first 5xx financial write behavior;
- partial-fill/cancel semantics;
- execution identity/history pagination;
- balances/positions/funding;
- private/public stream recovery policy;
- recursive secret redaction.

CI results must be taken from the exact pull-request head after this evidence is
committed. This document never turns a pending/cancelled workflow into PASS.

## Remaining WP-24 terminal evidence

Before any WP-24 capability can be marked QUALIFIED for LIVE:
- obtain separately authorized, redacted account capability evidence;
- prove the exact credential permission/IP binding without exposing secret values;
- bind durable nonce checkpoint persistence atomically to the canonical guarded
  send lifecycle and exercise restart boundaries;
- record actual quota/429/Retry-After behavior for the qualified endpoint/IP
  scope without exhausting safety reserves;
- qualify public/private WebSocket authentication, reconnect, gap, REST backfill
  and full-snapshot boundaries against recorded provider evidence;
- exercise order rejection, partial fill, cancel/fill race, timeout-after-send,
  duplicate/lost event and terminal correction scenarios;
- reconcile positions, spot/collateral balances, executions and external/manual
  activity across one coherent account snapshot;
- record exact source SHA, CI/test artifacts, environment and unresolved limits;
- keep any real-account probe behind separate explicit WP-58 authorization.

No credential use, withdrawal authority or live order is introduced by this
foundation.
