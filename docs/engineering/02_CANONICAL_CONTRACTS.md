# AutoTrade — canonical contracts

Contract baseline `autotrade.contracts/1.0.0`. Normative design, not production source. JSON Schema 2020-12 plus OpenAPI 3.1 are the implementation artifacts to commit first. C# and Python/TypeScript bindings are generated from the same schemas; examples are not alternative schemas.

## 1. Common types and compatibility

`Id`: opaque UUID string. `Digest`: algorithm prefix plus lowercase hex, initially `sha256:`. `UtcInstant`: RFC3339 UTC ending Z with up to seven fractional digits; source nanosecond timestamps additionally retain original integer text and resolution. `Sequence`: nonnegative integer text, avoiding JavaScript integer limits. `Decimal`: canonical string matching `^-?(0|[1-9][0-9]*)(\.[0-9]+)?$`, no exponent, plus sign, negative zero or trailing fractional zeros. C# admission additionally requires exact representation within decimal range/scale; reject rather than round if unsupported. Calculations use checked decimal or an explicitly approved larger exact type. Rounding is explicit per instrument and currency policy.

`Money = {amount: Decimal, currency: CurrencyId}`; `Quantity = {value: Decimal, unit: UnitId}`. Units distinguish base units, shares and contracts. Currency is not inferred from a symbol. `Environment = REPLAY | SIMULATION | PAPER | LIVE`; credentials and IDs are environment-separated. `EvidenceRef = {artifact_id, sha256, source_uri?, observed_at, rights_id?}`. Payloads cannot contain credentials or credential-shaped provider headers.

Major versions change meaning/required structure. Minor versions add optional fields with fixed defaults. Financial commands reject unknown fields and unknown enum values; observations preserve unknown provider data in the raw evidence object and quarantine unrecognized economically material events. Schema upgrades have forward/backward fixtures and a journal migration plan. No silent float coercion.

## 2. Durable event envelope

Required fields: `event_id`, `event_type`, `schema_version`, `aggregate_type`, `aggregate_id`, `aggregate_version`, `host_id`, `owner_epoch`, `environment`, `occurred_at`, `observed_at`, `committed_at`, `correlation_id`, `causation_id` (nullable only for external roots), `payload`, `payload_hash`, `evidence_refs[]`. Optional provider fields: `provider_id`, `account_id`, `provider_event_id`, `provider_sequence`, `source_resolution`.

`occurred_at` is source event time, not authority to know the event early. `observed_at` is first receipt by this system; historical imports retain separately evidenced `available_at`. `committed_at` records journal commit. Aggregate version is assigned by the writer. Unique `(provider, account, environment, event-kind, provider-event-id)` is used only where the provider guarantees that key's uniqueness; adapters declare correction/revision semantics. Otherwise use a documented composite with collision evidence, never time alone.

Append-only facts cannot be edited. Correction events reference and reverse prior facts. Hashes detect accidental mutation against trusted anchors; they do not defeat an attacker controlling both data and root hashes. Export signatures and independently retained anchors strengthen audit integrity.

## 3. Instruments and capabilities

`InstrumentVersion`: `instrument_id`, `version`, `provider_id`, `venue_id`, `provider_symbol`, `asset_class`, `base_currency`, `quote_currency`, `settlement_currency`, `quantity_unit`, `contract_multiplier`, `price_tick`, `quantity_step`, `minimum_quantity`, `minimum_notional?`, `maximum_quantity?`, `price_bands?`, `calendar_id`, `timezone_id`, `effective_from`, `effective_to?`, `status`, `metadata_evidence[]`.

Derivative extension: `payoff = LINEAR | INVERSE | OPTION`, `underlying_id`, `expiry?`, `last_trade_at?`, `delivery_cutoff?`, `settlement_method`, `funding_schedule?`, `strike?`, `option_right?`, `exercise_style?`, `deliverable[]`, `margin_model_id`. Perpetuals have funding and no invented expiry. Option deliverable can differ from a standard 100-share contract. A symbol is never a global identifier.

`CapabilitySnapshot`: `snapshot_id`, `provider_id`, `account_id`, `entity_id`, `environment`, `instrument_version?`, `observed_at`, `expires_at`, `supported_order_types[]`, `time_in_force[]`, `permission_scopes[]`, `position_mode`, `leverage_bounds?`, `shorting_rules?`, `reduce_only_semantics?`, `native_protection[]`, `rate_limit_policy_id`, `data_entitlements[]`, `evidence[]`, `status = VERIFIED | UNKNOWN | CONFLICTED | EXPIRED`.

Only VERIFIED and unexpired evidence can authorize the relevant action. Refresh follows provider change events plus bounded TTL; trading cannot rely on a stale cache after reconnect/account configuration change. Architecture does not infer capabilities from nationality or location. Instrument-specific capability intersection overrides broad provider marketing support.

## 4. Data contracts

`MarketEvent`: envelope + `instrument_version`, `kind = TRADE | QUOTE | BOOK_SNAPSHOT | BOOK_DELTA | BAR | FUNDING | MARK | INDEX | STATUS`, `source_event_at`, `available_at`, `availability_basis`, `ingested_at`, `source_sequence?`, `revision`, typed payload, `quality_flags[]`, `raw_evidence_ref`. A bar contains start/end, OHLCV, finalized flag and first availability; finalized close cannot be known at bar start. Book deltas contain predecessor/range IDs and checksum when supplied; a gap invalidates executable book state.

`InformationEvent`: `information_id`, `source_id`, `source_event_at?`, `published_at`, `available_at`, `ingested_at`, `revision`, `supersedes?`, `entities[]`, `claims[]`, `content_hash`, `rights_id`, `trust_features`, `language`, `evidence[]`. Store disagreements as claims from separate sources. A revised macro series does not replace its historical vintages.

`DatasetManifest`: `dataset_id`, `version`, `content_hashes[]`, `instrument_universe_version`, `calendar_version`, `coverage`, `availability_policy`, `revision_policy`, `normalization_version`, `adjustment_policy`, `rights`, `missingness_report`, `source_evidence[]`, `created_at`. Frozen experiments reference a manifest digest, not a folder name that can later change.

## 5. Decisions and portfolio targets

`DecisionProposal`: `proposal_id`, `strategy_version`, `model_version?`, `decision_at`, `information_cutoff`, `input_manifest_refs[]`, `thesis`, `candidate_instruments[]`, `horizon`, `expected_return_distribution_ref`, `confidence_basis`, `counterarguments[]`, `exit_policy_ref`, `expiry`, `estimated_compute_cost`, `NO_TRADE_reason?`. Language-model narrative never substitutes for numeric evidence.

`PortfolioTarget`: `target_id`, `account_scope[]`, `base_currency`, `as_of_state_version`, `target_exposures[]`, `cash_reserve`, `constraint_set_id`, `objective_version`, `proposal_refs[]`, `turnover_limit`, `estimated_costs`, `solver_status`, `fallback_reason?`. An infeasible or failed optimizer emits a no-increase target, not a partially parsed allocation.

`OrderIntent`: `intent_id`, `intent_version`, `account_id`, `environment`, `instrument_version`, `side`, `quantity`, `order_type`, `limit_price?`, `stop_price?`, `time_in_force`, `reduce_only`, `position_side?`, `parent_intent_id?`, `oco_group?`, `multi_leg_group?`, `maximum_cost`, `maximum_slippage`, `expires_at`, `decision_ref`, `portfolio_target_ref`, `capability_snapshot_id`, `expected_state_version`, `client_order_id`, `intent_hash`. Client ID mapping obeys provider length/charset rules and remains collision-resistant; cancellation/replacement gets separate command IDs and retains lineage.

## 6. Risk, authority and admission

`RiskDecision`: `decision_id`, `intent_hash`, `state_version`, `policy_version`, `capability_snapshot_id`, `evaluated_at`, `valid_until`, `verdict = ALLOW | REJECT | REQUIRE_REVIEW`, `checks[] = {rule_id, measured, limit, status, evidence}`, `reservation_delta`, `stress_result_ref`, `reason_codes[]`.

`AuthorityPolicy`: `policy_id`, `version`, `mode = CONFIRMATION | AUTONOMOUS`, `account_scope`, `environments`, `allowed_assets`, `allowed_actions`, `capital_bounds`, `risk_policy_id`, `model_allowlist`, `compute_budget`, `protective_action_policy`, `valid_from`, `expires_at?`, `revocation_epoch`, `actor_id`. Strategy learning cannot write this object.

`Confirmation`: `confirmation_id`, `actor_id`, `intent_hash`, `account_id`, `environment`, `max_quantity`, `max_total_cost`, `policy_version`, `expires_at`, `consumed_at?`. Any material change requires a new confirmation. Autonomous execution uses the same admission checks with a matching policy instead of per-order confirmation.

`AdmissionRecord`: `admission_id`, `intent_hash`, `risk_decision_id`, `authority_ref`, `reservation_id`, `owner_epoch`, `policy_revocation_epoch`, `expires_at`, `status`. It is an internal durable record, not a bearer token agents can manufacture. Only the financial writer creates it. The last send boundary rechecks status/epochs/expiry/capability/market freshness and worst-case reservation; a stale admission is rejected or re-evaluated atomically.

## 7. Provider interface

All methods accept cancellation/deadline, correlation ID and environment. Credentials are injected through a scoped secret handle, never passed through proposal payloads.

| Method | Input → output | Required semantics |
|---|---|---|
| DiscoverCapabilities | account, instrument scope → CapabilitySnapshot | Account evidence, expiry, unsupported distinctions |
| ListInstruments | cursor, as-of → page of InstrumentVersion | Stable pagination and version changes |
| SubscribeMarketData | subscription set, cursor? → MarketEvents | Gap/quality events, reconnect snapshots |
| FetchHistory | range, granularity, cursor → data page + coverage | Availability limits and rights; no invented missing ticks |
| FetchAccountSnapshot | scope → balances, positions, open orders, provider time/cursor | Snapshot consistency evidence; partial response is labelled |
| Submit | admitted OrderIntent → SubmissionResult | ACKNOWLEDGED / REJECTED / UNKNOWN; no fabricated fill |
| Cancel | order ref, command ID → action acknowledgement | Cancel request is not cancellation completion |
| Amend | order ref, new terms, admission → result + lineage | Explicit atomic amendment vs cancel/replace semantics |
| QueryOrder | provider/client ref → FOUND / PROVEN_ABSENT / INCONCLUSIVE | Proof basis, search coverage and consistency window |
| SubscribeAccountEvents | cursor? → order/fill/balance/lifecycle events | Dedupe keys and sequence/retention behavior |
| FetchExecutionsAndActivities | time/cursor window → pages + watermark | Fills, fees, funding, corrections, assignments and manual activity |
| Health | none → transport/auth/clock/quota/data health | No generic green status hiding account desync |

Error taxonomy: AUTH, PERMISSION, CAPABILITY, VALIDATION, INSUFFICIENT_FUNDS, RATE_LIMIT, STALE_DATA, TRANSIENT_READ, AMBIGUOUS_WRITE, PROVIDER_INTERNAL, SCHEMA_CHANGE. Only read-safe/idempotent operations retry automatically with bounded backoff and jitter. AMBIGUOUS_WRITE routes to reconciliation.

## 8. Execution and economic events

`SubmissionAttempt`: intent ref, attempt ID, owner/policy epoch, send-started-at, transport request fingerprint, provider/client IDs, response evidence, outcome and ambiguity reason. Do not persist authentication signatures.

`ExecutionFill`: `fill_id`, provider execution ID/revision, order/intent refs (nullable intent for external/manual fills), instrument version, side, last quantity, last price, trade time, receipt time, `fees[]`, liquidity flag, settlement date, correction reference, evidence. Cumulative quantities in order-status reports are consistency checks, not independent cash postings.

`JournalTransaction`: transaction ID, cause event ID, booking/effective dates, `postings[] = {ledger_account, asset_or_currency, signed_amount, valuation_ref?}`, reverses transaction ID?, evidence. Each asset/currency's double-entry postings balance under the accounting policy; FX conversion has explicit clearing and valuation postings. Unit inventory and value ledgers must not be added together. `PositionSnapshot`, `CashSnapshot`, `MarginSnapshot` each carry source, as-of, version and reconciliation status.

`ReconciliationRun`: scope, opening local version, provider watermarks, inspected windows/pages, matched/unmatched items, differences by type and amount, actions, closing version, verdict. An incomplete search can produce INCONCLUSIVE, never PROVEN_ABSENT.

## 9. Persistence and transaction protocol

Logical tables: event_log, aggregate_versions, command_dedup, instrument_versions, capability_snapshots, intents, admissions, reservations, outbox, submission_attempts, provider_observations, journal_transactions, journal_postings, reconciliation_runs, policies, confirmations, owner_fences, jobs, experiments, promotions, artifact_manifests. Mutable projection tables can be rebuilt; source facts cannot.

Unique keys: command `(actor, environment, idempotency_key)`; intent `(account, environment, intent_id, version)`; provider client ID `(provider, account, environment, client_order_id)`; outbox command ID; event ID; provider dedupe tuple when valid. Reuse of an idempotency key with a different payload hash is a conflict. Repetition with the same hash returns the original command result.

Transaction A: verify expected state/policy versions → evaluate final risk against current reservations → consume confirmation if required → append intent/admission/reservation/events/outbox → commit. No network send inside the database transaction.

Transaction B: single account dispatcher serializes with revocation, rechecks admission → marks attempt SEND_STARTED and commits → submits → appends response/outcome. The external call creates an unavoidable uncertainty window. A crash after SEND_STARTED is UNKNOWN until reconciled, even if the request might not have left the process.

Revocation linearization: the command succeeds when its new epoch is durable and the dispatcher has crossed a barrier rejecting all later sends under old authority. Sends already in the admitted in-flight set are listed to the user and followed to completion/cancel according to policy. Never promise that revocation can recall packets already sent.

Transaction C: dedupe provider observation → append fill/activity → balanced postings → update reservation/positions/order projection → append UI event → commit. A duplicate fill cannot post twice. Unknown remaining quantity stays reserved. Event publication uses durable sequence replay, not an in-memory notification as truth.

## 10. Learning, evaluation and UI contracts

`ExperienceEpisode`: decision/proposal/intent refs, information cutoff, input hashes, model/strategy versions, intended action, observed execution, realized outcome window, cost attribution, counterfactual policy and uncertainty, regime tags, failure tags, evidence. Corrections append a new episode revision. Predictions never overwrite outcomes.

`ModelArtifact`: immutable artifact digest, format, feature schema, training data hashes/cutoffs, algorithm/version, seeds, environment lock, model/weight license, risk classification, inference resource profile. `ExperimentProtocol`: registered hypothesis, train/validation/test/forward windows, purge/embargo rule, comparison baselines, trial budget, metric directions, acceptance thresholds, cost model, stopping rule, holdout access policy. `EvaluationResult`: protocol digest, all run hashes, metrics with intervals, failed checks, multiplicity/selection accounting, retention regression, cost and latency. `PromotionDecision`: candidate/prior versions, result refs, scope/envelope, independent gate signer, effective version and rollback target.

`ModelRequest`: role, task schema, data classification, allowed model IDs, privacy constraints, budget reservation, deadline, evidence references, allowed tool set (normally empty for inference). `ModelResponse`: exact provider/model revision or explicit unknown revision, structured result, validation verdict, token/compute usage, measured cost, latency, refusal/fallback/error. Model output cannot contain executable permissions.

`UiSnapshot`: state_version, event_cursor, server_time, host/account/environment identity, permission summary, connection freshness, portfolio/risk/strategy/job projections and accessible reason codes. `UiCommand`: command_id, expected_state_version, idempotency_key, actor/session, typed action and payload. Return ACCEPTED/REJECTED/CONFLICT with operation ID; accepted is not financially completed. SSE/WebSocket projection events are resumable; a gap triggers a snapshot. Desktop and web use these exact contracts.

## 11. Required contract fixtures

Commit fixtures before implementation: decimal/units; instrument versions and adjusted options; expired capability; stale confirmation; duplicate idempotency key with changed payload; partial fill + cancel race; UNKNOWN retry rejection; late fee/correction; manual order; book gap; revised macro/news; future bar; learning promotion outside envelope; model secret attempt; UI reconnect cursor gap; host ownership revocation; journal recovery after every transaction boundary. Every language and adapter must pass its applicable subset with the same expected outcomes.

## 12. Typed payload and response annex

The following closes common integration ambiguities. Fields below are required unless marked `?`. Decimal quantities always carry their instrument unit through the referenced instrument version; money amounts always name currency. Empty arrays are valid, omitted required arrays are not.

| Type / variant | Fields and constraints |
|---|---|
| Trade payload | `trade_id`, `price: Decimal > 0`, `quantity: Decimal > 0`, `aggressor: BUY/SELL/UNKNOWN`; negative-price-capable instruments use an explicitly enabled instrument price-domain policy instead of the default positive constraint |
| Quote payload | `bid_price?`, `bid_quantity?`, `ask_price?`, `ask_quantity?`; one-sided quotes explicit, quantities nonnegative, crossed status flagged rather than silently reordered |
| Book payload | `snapshot_id?`, `first_sequence`, `last_sequence`, `previous_sequence?`, `bids[]`, `asks[]`, `checksum?`; level `{price,quantity}`, zero quantity deletes a level in delta only; sort/check rules adapter-declared |
| Bar payload | `start`, `end`, `open`, `high`, `low`, `close`, `volume`, `is_final`, `first_available_at`; low ≤ open/close ≤ high, end > start; final availability no earlier than end unless source semantics prove an explicitly different meaning |
| Funding payload | `effective_at`, `rate`, `interval`, `reference_price?`, `sign_convention`, `settlement_currency`; future indicated rate and final charged rate have distinct event kinds/revisions |
| AccountSnapshot | `snapshot_id`, `account_id`, `environment`, `query_started_at`, `query_completed_at`, `provider_as_of?`, `stream_watermark?`, `consistency: ATOMIC/RECONSTRUCTED/PARTIAL`, `balances[]`, `positions[]`, `open_orders[]`, `margin`, `evidence[]` |
| Balance | `currency`, `total`, `settled?`, `unsettled?`, `available`, `reserved?`, `liability?`, `as_of`; provider definitions retained in adapter mapping; no assumed identity total=available+reserved when collateral rules differ |
| Position | `instrument_version`, `position_side: NET/LONG/SHORT`, `quantity`, `average_cost?`, `mark_price?`, `realized_pnl?`, `unrealized_pnl?`, `collateral?`, `source`, `as_of`; accounting versus provider quantities remain distinguishable |
| MarginSnapshot | `account_id`, `as_of`, `initial_requirement?`, `maintenance_requirement?`, `available_collateral?`, `liquidation_estimate?`, `model_id`, `source`, `quality`; missing provider estimates are unknown, not zero |
| SubmissionResult | `attempt_id`, `outcome: ACKNOWLEDGED/REJECTED/UNKNOWN`, `provider_order_id?`, `client_order_id`, `provider_received_at?`, `reason_code?`, `evidence[]`, `retry_disposition: NEVER/RECONCILE_FIRST/NEW_ADMISSION_REQUIRED`; fills arrive as separate execution events |
| QueryOrderResult | `verdict: FOUND/PROVEN_ABSENT/INCONCLUSIVE`, `order?`, `searched_surfaces[]`, `time_window`, `pagination_complete`, `consistency_horizon`, `evidence[]`; absence requires all declared proof conditions |
| Page<T> | `items[]`, `next_cursor?`, `coverage_start?`, `coverage_end?`, `complete`, `watermark?`, `evidence[]`; end-of-page is not necessarily complete coverage |
| CommandResult | `command_id`, `operation_id?`, `status: ACCEPTED/REJECTED/CONFLICT`, `state_version`, `reason_codes[]`, `field_errors[]`, `current_value_ref?`; financial completion is queried separately |
| OperationResult | `operation_id`, `phase: QUEUED/RUNNING/WAITING_EXTERNAL/SUCCEEDED/FAILED/UNKNOWN/CANCELLED`, `started_at`, `updated_at`, `affected_refs[]`, `evidence[]`, `remaining_uncertainty[]`; UI announces this exact phase |
| HealthResult | `component`, `as_of`, `status: READY/RECOVERING/DEGRADED/BLOCKED/STOPPED`, `affected_scope`, `reason_codes[]`, `last_good_at?`, `next_action?`; liveness and readiness remain separate |
| Job record | `job_id`, `kind`, `input_hashes[]`, `dedupe_key`, `state`, `owner?`, `lease_until?`, `generation`, `attempt`, `checkpoint_ref?`, `resource_budget`, `output_refs[]`, `error?` |
| GateProfile | `profile_id`, `version`, `registered_at`, `primary_metric`, `direction`, `baseline`, `minimum_practical_effect`, `uncertainty_method`, `confidence_level`, `selection_correction`, `power_or_precision_plan`, `risk_limits`, `retention_limits`, `cost_model_ref`, `stopping_rule`, `required_regime_coverage`, `digest` |

Enums for asset class are `CASH_EQUITY`, `FUND`, `FX`, `CRYPTO_SPOT`, `FUTURE`, `PERPETUAL`, `OPTION`, with new types requiring schema review. Margin is an account/position financing mode, not a substitute asset identity. Initial order types are MARKET, LIMIT, STOP_MARKET, STOP_LIMIT, TRAILING_STOP; bracket/OCO/multi-leg are explicit relationships with capability-defined atomicity. Time-in-force is DAY, GTC, IOC, FOK, GTD with required expiry for GTD. A provider-specific order type cannot be approximated silently; add a versioned supported extension or reject.

Reason codes are stable namespaced strings, including `RISK.INSUFFICIENT_AVAILABLE`, `RISK.LIMIT_BREACH`, `DATA.STALE`, `DATA.SEQUENCE_GAP`, `AUTH.EXPIRED`, `AUTH.REVOKED`, `AUTH.INTENT_CHANGED`, `CAPABILITY.UNKNOWN`, `CAPABILITY.UNSUPPORTED`, `EXECUTION.AMBIGUOUS_SEND`, `RECONCILIATION.INCOMPLETE`, `SCIENCE.LEAKAGE`, `SCIENCE.INCONCLUSIVE`, `MODEL.BUDGET_EXHAUSTED`, `MODEL.PRIVACY_DENIED`, `STATE.VERSION_CONFLICT`. UI localization maps codes to text; parsing human text is never required for control flow.

Protocol serialization: UTF-8 JSON for commands/events; newline-delimited envelopes only for exports/stream framing, not a replacement for transactional storage. Internal research RPC uses the same schemas over authenticated local HTTP or a bounded named-pipe transport; large datasets/artifacts pass by immutable manifest reference. Python and model workers cannot connect to trade endpoints. All calls declare deadlines and bounded payload sizes; large historical transport uses Parquet/Arrow manifests. These transport choices preserve one canonical semantic contract.
