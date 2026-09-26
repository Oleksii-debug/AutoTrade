# WhiteBIT adapter foundation evidence — 2026-09-26

Status: **implementation / deterministic-fixture foundation only; NOT QUALIFIED for LIVE provider authority and NOT evidence of a WhiteBIT paper/testnet environment**.

This is the single canonical WP-24 foundation note for issue #494. It records evidence that can be checked without credentials or network writes. It does not authorize credentials, provider probes, real orders, or LIVE trading.

## Exact identities

- AutoTrade implementation source revision covered by this evidence: `527a19441ac3338d740f9feaffae2339b3dfb8d3`.
- WhiteBIT official API documentation repository snapshot inspected: `whitebit-exchange/api-docs@fc217867a35de12118319c1ced7b1ae126b328e8`.
- Documentation snapshot inspected on: 2026-09-26.
- Changelog data at that documentation snapshot includes entries through 2025-09-22.
- Qualification signature: **ABSENT**.
- Exact-head CI result for this candidate lineage: **PENDING** when this note was written.
- Real-account / production-equivalent sandbox evidence: **ABSENT**.

The absence of a signature, terminal exact-head CI and provider-environment evidence means this document MUST NOT be interpreted as a QUALIFIED/PASS provider decision.

Canonical implementation/tests covered here:
- `mvp/autotrade_mvp/whitebit.py`
- `mvp/autotrade_mvp/provider_transport.py`
- `mvp/tests/test_whitebit_adapter.py`
- `mvp/tests/test_provider_transport.py`

## Canonical AutoTrade requirements

`docs/engineering/03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md` requires actual account capability evidence rather than geography/product assumptions; endpoint/account-scoped quotas with protected recovery/cancel capacity; bounded safe read retry; reconciliation before retry after a possibly-sent financial write; clock-drift fencing; ACK distinct from fill; UNKNOWN reconciliation; and provider qualification tied to exact evidence.

Local simulation or deterministic fixtures are not re-labelled as provider parity.

## Official provider documentation basis

The source contract was checked against the official WhiteBIT documentation snapshot above, including:

- `pages/private/http-auth.mdx`: POST authentication, monotonically increasing nonce, optional `nonceWindow`, millisecond nonce guidance, ±5-second server-time window, uniqueness, HMAC-SHA512 signing, IP restriction and endpoint restriction guidance.
- `pages/private/http-trade-v4.mdx`: private trade endpoints, endpoint-specific rate limits, create/cancel/history contracts and documented HTTP response classes.
- `pages/faq.mdx`: 429 rate-limit behavior, wait-for-window guidance, minimum-permission/IP-restriction security guidance and reconnect/backoff guidance.
- `public/data/changelog.json`: provider API/WebSocket change history, including stream and hedge-mode changes.
- the source-level `WHITEBIT_OFFICIAL_DOCS` references.

Documentation statements are provider contract inputs, not runtime proof that an account currently has a capability or that an endpoint is healthy.

## Environment matrix

| Environment | AutoTrade evidence use | Current provider qualification |
| --- | --- | --- |
| REPLAY | Recorded/local deterministic fixtures | Foundation only; not a WhiteBIT environment |
| SIMULATION | Local simulator/deterministic fixtures | Foundation only; not a WhiteBIT environment |
| PAPER | AutoTrade semantic environment only | NOT QUALIFIED; no proven production-equivalent WhiteBIT sandbox here |
| LIVE | Real WhiteBIT service/account | NOT QUALIFIED; no authorized credential/probe/order evidence in this lineage |

No test, demo, paper, or local result may be promoted to LIVE evidence.

## Credential boundary

The branch includes a machine-checked `WhiteBitCredentialBoundary` policy model requiring the intended minimum AutoTrade trading authority:

- exact non-secret credential-binding identity;
- `INFO` + `TRADING` only;
- `DEPOSIT` / `WITHDRAW` fund-movement authority forbidden;
- unknown permission names fail closed;
- actual provider credential environment must be LIVE for any future LIVE qualification;
- IP allowlisting required by AutoTrade policy;
- secrets never enter this evidence model.

This is a policy/test contract. It is **not runtime proof** that any configured WhiteBIT credential currently satisfies those restrictions. Endpoint-level restriction and actual account capability still require provider/account evidence.

## Capability matrix

| Surface | Deterministic implementation evidence | Current qualification |
| --- | --- | --- |
| SPOT | exact decimals; market metadata; market/limit/stop request shapes; order/execution history; balances; recovery policy | Foundation only |
| COLLATERAL | collateral request shapes; reduce-only preservation; positions; balances/borrow; funding; hedge-mode evidence | Foundation only |
| FUTURES | futures market classification; position/funding/fee primitives; position-side evidence | Foundation only |
| TRADFIFUTURES execution | explicit source rejection where provider capability is not established | Unsupported / fail closed |
| guarded order submission | existing one-shot LIVE transport; ambiguous send becomes UNKNOWN/reconcile | Source behavior only |
| order/execution history | pagination/normalization/reconciliation fixtures | Foundation only |
| WebSocket recovery | endpoint validation and recovery checkpoint/policy fixtures | Foundation only |

The matrix is not a real-account capability claim. Unsupported or account-specific semantics remain fail-closed.

## Canonical nonce ownership, restart and concurrent send ordering

There is exactly one production nonce authority for WhiteBIT: the existing journal-backed `WhiteBitDurableNonceAllocator` in `provider_transport.py`.

The earlier branch-local in-memory `WhiteBitNonceState/WhiteBitNonceAllocator` experiment has been removed; it is not production evidence and must not reappear as a parallel nonce authority.

The canonical WhiteBIT transport now holds the existing serialized-send lock from durable nonce allocation through signing, the final execution guard and the single wire send. This closes the race where a concurrently allocated nonce `n+1` could otherwise reach WhiteBIT before `n`.

Relevant deterministic regressions include:

- `test_durable_nonce_survives_restart_and_clock_regression`
- `test_whitebit_transport_has_one_guarded_send_after_durable_nonce`
- `test_whitebit_concurrent_sends_cannot_overtake_nonce_order`
- `test_whitebit_final_guard_failure_never_reaches_wire`
- `test_private_signer_uses_exact_body_and_caller_owned_nonce`
- `test_nonce_window_requires_current_server_time_evidence`
- `test_private_signer_rejects_nonce_conflict_and_binary_float`

Provider-side clock skew and nonce acceptance on a real account remain unqualified.

## Rate-limit and retry contract

`WhiteBitRateLimitBudget` takes an evidenced endpoint/window capacity from its caller. It does not assume one provider-wide quota. Normal traffic preserves recovery and cancel reserves; recovery preserves cancel capacity; cancel has highest admission priority.

`classify_whitebit_http_retry` deliberately preserves the canonical UNKNOWN semantics:

- 429 READ/RECOVERY: bounded backoff with jitter may be admitted;
- 429 WRITE/CANCEL: **no automatic retry; reconciliation required**;
- 5xx READ/RECOVERY: bounded backoff may be admitted;
- 5xx WRITE/CANCEL: **no automatic retry; reconciliation required**;
- authentication/client-validation classes: no automatic retry.

Relevant regressions:
- `test_rate_budget_preserves_recovery_and_cancel_capacity`
- `test_retry_policy_backs_off_safe_reads_without_blind_financial_write_retry`

Actual rate-limit feedback, endpoint/IP quota measurement and provider-side retry behavior remain unqualified until separately observed.

## Submission and economic truth

The existing submission parser treats a successful create response only as ACKNOWLEDGED, never as a fill. A possibly-sent request without a qualified definitive response becomes UNKNOWN and requires reconciliation before retry.

Deterministic fixtures include:

- `test_submission_success_is_acknowledged_not_fill`
- `test_ambiguous_transport_requires_reconcile_before_retry`
- `test_taker_band_cancellation_is_partial_fill_not_failure`
- `test_external_order_without_client_id_remains_provider_truth`
- `test_unique_execution_deal_maps_to_reconciliation_fill`
- `test_execution_history_deduplicates_exact_rows_and_rejects_conflicts`
- `test_funding_fingerprint_is_deterministic_but_changes_with_correction`
- `test_whitebit_prepared_request_flows_through_dispatcher_and_ambiguity_never_retries`

These reduce implementation risk but do not prove full lifecycle completeness against a real provider account.

## Stream/recovery evidence

Deterministic fixtures include:

- `test_current_websocket_host_is_required`
- `test_positions_recover_only_after_new_full_snapshot`
- `test_incremental_balance_requires_baseline_and_subscription`
- `test_event_stream_reconnect_requires_backfill_not_just_resubscribe`

They prove local recovery semantics only. Production stream retention, authentication, reconnect timing, sequence-gap behavior and provider-side backfill completeness remain unqualified.

## Focused and repository gates

Useful deterministic commands include:

- `python -m unittest mvp.tests.test_whitebit_adapter -v`
- `python -m unittest mvp.tests.test_provider_transport.WhiteBitProviderTransportTests -v`
- `python tools/verify.py`

Results must be taken from the exact final head. A queued, cancelled, stale, or different-head workflow is never PASS.

## Remaining terminal WP-24 evidence

WP-24 remains **NOT QUALIFIED** until all applicable terminal evidence is present without weakening safety:

1. terminal exact-head baseline, reconvergence-integrity and full Verify AutoTrade;
2. a redacted machine-readable qualification artifact binding exact source/docs/test evidence;
3. independent signature verification for the terminal qualification artifact;
4. exact provider/account/environment capability evidence;
5. actual credential restriction evidence without exposing secret material;
6. provider-side quota/429 behavior with reserved recovery/cancel capacity;
7. public/private stream authentication, reconnect, gap/backfill and snapshot completeness evidence;
8. order rejection, partial fill, cancel/fill race, timeout-after-send, duplicate/lost event and terminal correction evidence;
9. coherent positions/balances/orders/executions/manual-activity reconciliation for the exact account/environment scope;
10. explicit test-environment evidence. If no production-equivalent WhiteBIT sandbox exists for the required surfaces, that absence remains explicit rather than becoming PAPER qualification;
11. any real-account probe remains separately authorized and bounded; this foundation grants no such authority.

## Current decision

**IMPLEMENTATION / RECORDED-FIXTURE FOUNDATION ONLY — NOT QUALIFIED.**

The current branch materially strengthens nonce send ordering and no-blind-retry behavior while reusing canonical authorities. It does not change WhiteBIT real-account or LIVE-trading authority.
