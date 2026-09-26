# WhiteBIT provider qualification candidate

Status: **CANDIDATE_NOT_QUALIFIED**

This document is an evidence-bound qualification candidate for WP-24 / issue #494. It does not authorize credentials, provider probes, real orders, or LIVE trading.

## Exact identities

- AutoTrade implementation source SHA under review: `527a19441ac3338d740f9feaffae2339b3dfb8d3`.
- WhiteBIT official API documentation repository snapshot: `whitebit-exchange/api-docs@fc217867a35de12118319c1ced7b1ae126b328e8`.
- Documentation snapshot inspected on: 2026-09-26.
- Changelog data at that documentation snapshot includes entries through 2025-09-22.
- Qualification signature: **ABSENT**.
- Exact-head CI result for this candidate lineage: **PENDING** at report creation.
- Real-account / production-equivalent sandbox evidence: **ABSENT**.

The absence of a signature, terminal exact-head CI, and provider-environment evidence means this report MUST NOT be interpreted as a QUALIFIED or PASS provider decision.

## Official documentation basis

The source contract is grounded in the official WhiteBIT documentation snapshot above, including:

- `pages/private/http-auth.mdx`: POST authentication, monotonically increasing nonce, optional `nonceWindow`, ±5-second time window, uniqueness, HMAC-SHA512 request signing, IP restrictions and endpoint restrictions.
- `pages/private/http-trade-v4.mdx`: private trade endpoints, endpoint-specific rate limits, create/cancel/history contracts, 200/400/422/503 response classes, client order identity and collateral/futures surfaces.
- `pages/faq.mdx`: 429 rate-limit behavior, wait-for-window guidance, minimum-permission/IP-restriction security guidance, reconnect/backoff guidance.
- `public/data/changelog.json`: provider API/websocket change history, including hedge-mode and stream update changes.
- AutoTrade source references in `WHITEBIT_OFFICIAL_DOCS`.

No documentation statement is treated as runtime proof of account capability or endpoint availability.

## Environment matrix

| Environment | AutoTrade use | Evidence state | Qualification |
| --- | --- | --- | --- |
| REPLAY | Recorded/local fixtures only | Source + deterministic tests | Foundation only |
| SIMULATION | Local simulator only | Source + deterministic tests | Foundation only |
| PAPER | AutoTrade semantic environment only; no proven WhiteBIT production-equivalent public sandbox | No provider-equivalent network evidence | NOT QUALIFIED |
| LIVE | Real WhiteBIT account | No authorized credential/probe/order evidence in this lineage | NOT QUALIFIED |

A WhiteBIT LIVE credential or order probe requires separate authorization/evidence. This WP does not grant it.

## Credential boundary

Required AutoTrade policy for any future WhiteBIT LIVE qualification:

- minimum provider authority required for trading only;
- INFO + TRADING capability only;
- deposit/withdraw/fund-movement authority excluded;
- IP allowlisting required;
- endpoint restriction required to the minimum admitted surfaces;
- secrets must remain behind the existing scoped credential boundary and must never enter qualification artifacts.

The branch contains a `WhiteBitCredentialBoundary` validation model and tests for this required policy. It is **not yet runtime proof that a real configured WhiteBIT key has these settings**. No provider key metadata or secret is present in this report.

## Capability matrix

| Surface | Local implementation evidence | Current qualification |
| --- | --- | --- |
| SPOT order preparation | MARKET/LIMIT/STOP_MARKET/STOP_LIMIT with explicit representation limits, exact Decimal rules, capability binding, client-order identity | Foundation only |
| COLLATERAL order preparation | Limit/market/conditional/reduce-only/position-side representation where admitted by exact capability evidence | Foundation only |
| FUTURES / position economics | Position, hedge-mode, funding and fee normalization primitives exist | Foundation only |
| TRADFIFUTURES execution | Explicitly rejected by source where provider capability is not established | Unsupported / fail closed |
| Order submission | Guarded one-shot LIVE transport exists; ambiguous send is UNKNOWN and requires reconciliation | Source-qualified behavior only |
| Order/execution history | Pagination/normalization/reconciliation primitives and deterministic fixtures exist | Foundation only |
| Balances | Spot + collateral balance parsing/request shapes | Foundation only |
| WebSocket recovery | Explicit endpoint validation and recovery policy/checkpoint fixtures | Foundation only |

No table row above is a real-account capability claim.

## Authentication, nonce and concurrency evidence

Canonical nonce ownership remains in `WhiteBitDurableNonceAllocator` in `provider_transport.py`; no second nonce authority is admitted.

Relevant deterministic tests include:

- `test_durable_nonce_survives_restart_and_clock_regression`
- `test_whitebit_transport_has_one_guarded_send_after_durable_nonce`
- `test_whitebit_concurrent_sends_cannot_overtake_nonce_order`
- `test_whitebit_final_guard_failure_never_reaches_wire`
- `test_private_signer_uses_exact_body_and_caller_owned_nonce`
- `test_nonce_window_requires_current_server_time_evidence`
- `test_private_signer_rejects_nonce_conflict_and_binary_float`

The canonical WhiteBIT transport now holds the existing cross-thread/process serialized-send lock from durable nonce allocation through signing, final guard and the single wire send. This prevents a later nonce from reaching the provider before an earlier allocated nonce from the same durable domain.

Provider-side clock skew and nonce acceptance on a real account remain unqualified.

## Rate-limit and retry evidence

The branch contains deterministic quota partitioning that preserves reserved recovery and cancel capacity. It does not assume one universal WhiteBIT rate because the official API documents endpoint-specific limits.

Relevant tests:

- `test_rate_budget_preserves_recovery_and_cancel_capacity`
- `test_retry_policy_backs_off_safe_reads_without_blind_financial_write_retry`

Safety rule:

- 429 on READ/RECOVERY may be classified for bounded backoff.
- 429 or 5xx on financial WRITE/CANCEL is not blind-retried by this policy; it requires reconciliation first.
- The existing order submission parser treats only specifically qualified provider responses as definitive and otherwise fails toward UNKNOWN/reconciliation.

Real rate-limit feedback and adaptive quota measurement are not provider-qualified in this lineage.

## Stream/recovery evidence

Deterministic fixtures include:

- `test_current_websocket_host_is_required`
- `test_positions_recover_only_after_new_full_snapshot`
- `test_incremental_balance_requires_baseline_and_subscription`
- `test_event_stream_reconnect_requires_backfill_not_just_resubscribe`

These prove local recovery policy behavior only. They do not prove production stream retention, reconnect timing, sequence-gap behavior or provider-side backfill completeness.

## Order/economic evidence

Existing deterministic source fixtures cover, among other things:

- acknowledgement is not treated as a fill;
- timeout/ambiguous transport requires reconciliation;
- external orders without AutoTrade client IDs remain provider truth;
- partial-fill/terminal status normalization;
- execution-deal dedupe/conflict detection;
- exact economic identity requirements;
- position identity and hedge-mode evidence;
- exact spot/collateral cash semantics;
- fee and funding normalization/correction fingerprints.

Relevant tests include:

- `test_submission_success_is_acknowledged_not_fill`
- `test_ambiguous_transport_requires_reconcile_before_retry`
- `test_taker_band_cancellation_is_partial_fill_not_failure`
- `test_external_order_without_client_id_remains_provider_truth`
- `test_unique_execution_deal_maps_to_reconciliation_fill`
- `test_execution_history_deduplicates_exact_rows_and_rejects_conflicts`
- `test_funding_fingerprint_is_deterministic_but_changes_with_correction`
- `test_whitebit_prepared_request_flows_through_dispatcher_and_ambiguity_never_retries`

The terminal WP-24 evidence requirement is still stricter than these local fixtures: a coherent provider-account qualification package must prove lifecycle/reconciliation completeness for the exact admitted provider/account/environment matrix.

## Explicit open qualification gates

WP-24 remains **NOT QUALIFIED** until all of the following are evidenced without weakening safety:

1. Terminal exact-head baseline, reconvergence-integrity and full Verify AutoTrade for the final candidate SHA.
2. A redacted machine-readable qualification artifact whose hashes bind the exact source/docs/test evidence.
3. Independent signature verification of that artifact.
4. Provider-environment evidence. If WhiteBIT has no production-equivalent public sandbox suitable for these surfaces, that fact remains explicit rather than being reinterpreted as PAPER qualification.
5. Any real-account observation must be separately authorized and must prove the exact credential restrictions, endpoint matrix, account scope and evidence rights.
6. Provider-side lifecycle/reconciliation evidence for the exact capability subset being promoted.
7. No credentials, secrets, withdrawal authority or uncontrolled fund-movement capability in evidence or runtime admission.

## Current decision

**CANDIDATE_NOT_QUALIFIED.**

The source/fixture work reduces WP-24 implementation risk, including nonce send-order and no-blind-retry safety. It does not yet satisfy the terminal provider qualification rule and therefore does not change WhiteBIT real-account or LIVE-trading authority.
