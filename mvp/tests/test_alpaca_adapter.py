from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.alpaca import (
    AlpacaAbsenceEvidence,
    AlpacaAdapterError,
    AlpacaOrderIntent,
    AlpacaPreparedRequest,
    paper_evidence_proves_live_execution_realism,
    coverage_evidence,
    parse_order_observation,
    parse_submission_response,
    parse_trade_activities,
    prepare_order_request,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
    stable_client_order_id,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    observe_submission_json_response,
    prepare_authenticated_read_query,
)


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def capability(
    *,
    order_types=("MARKET", "LIMIT", "STOP", "STOP_LIMIT"),
    tif=("DAY", "GTC", "IOC"),
    account_id="paper-account",
    environment="PAPER",
    instrument_version="AAPL:v1",
):
    observed_at = NOW - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="ALPACA",
            account_id=account_id,
            entity_id="alpaca",
            environment=environment,
            instrument_version=instrument_version,
            observed_at=observed_at,
            expires_at=NOW + timedelta(hours=1),
            supported_order_types=frozenset(order_types),
            time_in_force=frozenset(tif),
            permission_scopes=frozenset({"ORDER_WRITE", "ORDER.READ"}),
            position_mode="NET",
            native_protection=frozenset({"STOP"}),
            rate_limit_policy_id="alpaca-paper-test",
            data_entitlements=frozenset({"ORDERS", "ACTIVITIES"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "d" * 64,
                "observed_at": "2026-09-24T19:00:00Z",
                "source_uri": "https://docs.alpaca.markets/us/reference/postorder",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=NOW,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def bound_activity_response(
    activities,
    *,
    account_id="paper-1",
    environment="PAPER",
):
    query = prepare_authenticated_read_query(
        capability=capability(
            account_id=account_id,
            environment=environment,
            instrument_version="AAPL:v1",
        ),
        surface=Surface.ACTIVITIES,
        endpoint="/v2/account/activities/FILL",
        query={"activity_types": "FILL"},
        at=NOW,
        permission_scope="ORDER.READ",
    )
    raw = json.dumps(
        activities,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return observe_authenticated_json_response(
        query_binding=query,
        http_status=200,
        response_bytes=raw,
        observed_at=NOW,
    )


class AlpacaAdapterTests(unittest.TestCase):
    def test_direct_prepared_request_cannot_bypass_scope_or_provenance(self):
        with self.assertRaisesRegex(
            AlpacaAdapterError,
            "canonical preparation factory",
        ):
            AlpacaPreparedRequest(
                endpoint="/v2/orders",
                body={
                    "symbol": "AAPL",
                    "side": "buy",
                    "type": "market",
                    "time_in_force": "day",
                    "client_order_id": "at-direct",
                    "qty": "1",
                    "extended_hours": False,
                },
                account_id="paper-account",
                environment="PAPER",
                capability_snapshot_id=str(uuid4()),
                documentation_refs=("https://docs.alpaca.markets/orders",),
                instrument_versions=("AAPL:v1",),
            )

    def test_equity_limit_request_preserves_decimal_strings(self):
        intent = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="aapl",
            side="buy",
            order_type="limit",
            time_in_force="day",
            quantity="1.25",
            limit_price="220.10",
        )
        request = prepare_order_request(
            intent,
            client_order_id="at-equity-1",
            account_id="paper-account",
            environment="PAPER",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/v2/orders")
        self.assertEqual(request.account_id, "paper-account")
        self.assertEqual(request.environment, "PAPER")
        self.assertEqual(request.instrument_versions, ("AAPL:v1",))
        self.assertEqual(request.capability_snapshot_ids, (request.capability_snapshot_id,))
        self.assertRegex(request.body_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(request.body["qty"], "1.25")
        self.assertEqual(request.body["limit_price"], "220.10")
        self.assertNotIn("notional", request.body)

    def test_order_preparation_is_bound_to_exact_account_and_environment(self):
        intent = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "account"):
            prepare_order_request(
                intent,
                client_order_id="at-wrong-account",
                account_id="other-account",
                environment="PAPER",
                capability=capability(),
                at=NOW,
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "environment"):
            prepare_order_request(
                intent,
                client_order_id="at-wrong-env",
                account_id="paper-account",
                environment="LIVE",
                capability=capability(environment="PAPER"),
                at=NOW,
            )

    def test_crypto_notional_uses_native_crypto_tif(self):
        intent = AlpacaOrderIntent.create(
            instrument_version="BTCUSD:v1",
            asset_class="CRYPTO",
            symbol="BTC/USD",
            side="BUY",
            order_type="MARKET",
            time_in_force="GTC",
            notional="100",
        )
        self.assertEqual(intent.notional, Decimal("100"))

    def test_crypto_notional_and_quantity_are_mutually_exclusive(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "exactly one"):
            AlpacaOrderIntent.create(
                instrument_version="BTCUSD:v1",
                asset_class="CRYPTO",
                symbol="BTC/USD",
                side="BUY",
                order_type="MARKET",
                time_in_force="GTC",
                quantity="0.01",
                notional="100",
            )

    def test_crypto_stop_requires_stop_limit_shape(self):
        intent = AlpacaOrderIntent.create(
            instrument_version="BTCUSD:v1",
            asset_class="CRYPTO",
            symbol="BTC/USD",
            side="SELL",
            order_type="STOP_LIMIT",
            time_in_force="GTC",
            quantity="0.01",
            limit_price="55000",
            stop_price="56000",
        )
        self.assertEqual(intent.order_type, "STOP_LIMIT")
        with self.assertRaises(AlpacaAdapterError):
            AlpacaOrderIntent.create(
                instrument_version="BTCUSD:v1",
                asset_class="CRYPTO",
                symbol="BTC/USD",
                side="SELL",
                order_type="STOP",
                time_in_force="GTC",
                quantity="0.01",
                stop_price="56000",
            )

    def test_option_quantity_is_whole_and_notional_is_forbidden(self):
        intent = AlpacaOrderIntent.create(
            instrument_version="AAPL_OPT:v1",
            asset_class="OPTION",
            symbol="AAPL261218C00250000",
            side="BUY",
            order_type="LIMIT",
            time_in_force="GTC",
            quantity="2",
            limit_price="5.25",
            position_intent="buy_to_open",
        )
        self.assertEqual(intent.quantity, Decimal("2"))
        with self.assertRaisesRegex(AlpacaAdapterError, "whole"):
            AlpacaOrderIntent.create(
                instrument_version="AAPL_OPT:v1",
                asset_class="OPTION",
                symbol="AAPL261218C00250000",
                side="BUY",
                order_type="LIMIT",
                time_in_force="DAY",
                quantity="1.5",
                limit_price="5.25",
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "notional"):
            AlpacaOrderIntent.create(
                instrument_version="AAPL_OPT:v1",
                asset_class="OPTION",
                symbol="AAPL261218C00250000",
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                notional="500",
            )

    def test_extended_hours_allows_documented_gtc_equity_limit(self):
        allowed = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="LIMIT",
            time_in_force="GTC",
            quantity="1",
            limit_price="220",
            extended_hours=True,
        )
        self.assertTrue(allowed.extended_hours)

    def test_extended_hours_is_conservative_day_equity_limit_only(self):
        allowed = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="LIMIT",
            time_in_force="DAY",
            quantity="1",
            limit_price="220",
            extended_hours=True,
        )
        self.assertTrue(allowed.extended_hours)
        with self.assertRaisesRegex(AlpacaAdapterError, "extended_hours"):
            AlpacaOrderIntent.create(
                instrument_version="AAPL:v1",
                asset_class="EQUITY",
                symbol="AAPL",
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity="1",
                extended_hours=True,
            )

    def test_binary_float_money_is_rejected(self):
        with self.assertRaises(AlpacaAdapterError):
            AlpacaOrderIntent.create(
                instrument_version="AAPL:v1",
                asset_class="EQUITY",
                symbol="AAPL",
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity=1.0,
            )

    def test_capability_evidence_controls_admission(self):
        intent = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="LIMIT",
            time_in_force="DAY",
            quantity="1",
            limit_price="220",
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "capability"):
            prepare_order_request(
                intent,
                client_order_id="at-order-1",
                account_id="paper-account",
                environment="PAPER",
                capability=capability(order_types=("MARKET",)),
                at=NOW,
            )

    def test_order_observation_is_not_unique_fill_evidence(self):
        observed = parse_order_observation(
            {
                "id": "order-1",
                "client_order_id": "at-order-1",
                "symbol": "AAPL",
                "status": "filled",
                "filled_qty": "1",
                "filled_avg_price": "220.10",
            }
        )
        self.assertEqual(observed.filled_quantity, Decimal("1"))
        self.assertFalse(observed.proves_economic_fill)

    def test_average_fill_without_quantity_fails_closed(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "filled_avg_price"):
            parse_order_observation(
                {
                    "id": "order-1",
                    "client_order_id": "at-order-1",
                    "symbol": "AAPL",
                    "status": "new",
                    "filled_qty": "0",
                    "filled_avg_price": "220.10",
                }
            )

    def test_positive_filled_quantity_requires_average_price(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "filled_avg_price is required"):
            parse_order_observation(
                {
                    "id": "order-1",
                    "client_order_id": "at-order-1",
                    "symbol": "AAPL",
                    "status": "partially_filled",
                    "filled_qty": "0.5",
                    "filled_avg_price": None,
                }
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "filled_avg_price is required"):
            parse_order_observation(
                {
                    "id": "order-1",
                    "client_order_id": "at-order-1",
                    "symbol": "AAPL",
                    "status": "filled",
                    "filled_qty": "1",
                    "filled_avg_price": "",
                }
            )

    def test_missing_filled_quantity_is_not_silently_zero(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "filled_qty is required"):
            parse_order_observation(
                {
                    "id": "order-1",
                    "client_order_id": "at-order-1",
                    "symbol": "AAPL",
                    "status": "new",
                }
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "filled_qty is required"):
            parse_order_observation(
                {
                    "id": "order-1",
                    "client_order_id": "at-order-1",
                    "symbol": "AAPL",
                    "status": "new",
                    "filled_qty": None,
                }
            )

    def test_null_provider_identity_fields_fail_closed_instead_of_stringifying(self):
        base = {
            "id": "order-1",
            "client_order_id": "at-order-1",
            "symbol": "AAPL",
            "status": "new",
            "filled_qty": "0",
        }
        for field in ("id", "client_order_id", "symbol", "status"):
            with self.subTest(field=field):
                payload = dict(base)
                payload[field] = None
                with self.assertRaises((AlpacaAdapterError, ValueError, TypeError)):
                    parse_order_observation(payload)

    def test_absence_needs_orders_trade_events_activities_and_horizon(self):
        partial = AlpacaAbsenceEvidence(
            by_client_order_id_complete=True,
            orders_history_complete=True,
            trade_events_complete=True,
            activities_complete=False,
            consistency_horizon_satisfied=True,
            order_found=False,
        )
        self.assertEqual(partial.verdict(), "INCONCLUSIVE")
        complete_but_unqualified = AlpacaAbsenceEvidence(
            by_client_order_id_complete=True,
            orders_history_complete=True,
            trade_events_complete=True,
            activities_complete=True,
            consistency_horizon_satisfied=True,
            order_found=False,
        )
        self.assertEqual(complete_but_unqualified.verdict(), "INCONCLUSIVE")
        qualified = AlpacaAbsenceEvidence(
            by_client_order_id_complete=True,
            orders_history_complete=True,
            trade_events_complete=True,
            activities_complete=True,
            consistency_horizon_satisfied=True,
            order_found=False,
            qualified_exclusion_semantics=True,
        )
        self.assertEqual(qualified.verdict(), "PROVEN_ABSENT")


    def _durable_submission_observation(
        self,
        *,
        payload,
        intent_id="alpaca-submission-intent",
        attempt_id=None,
    ):
        attempt = attempt_id or str(uuid4())
        client_id = stable_client_order_id(
            "ALPACA",
            intent_id,
            environment="PAPER",
            account_id="paper-account",
        )
        intent = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        prepared = prepare_order_request(
            intent,
            client_order_id=client_id,
            account_id="paper-account",
            environment="PAPER",
            capability=capability(),
            at=NOW,
        )
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="paper-account",
                owner_token="owner",
            )
            outcome = dispatcher.dispatch(
                attempt_id=attempt,
                intent_id=intent_id,
                intent_hash="alpaca-intent-hash",
                provider="ALPACA",
                request=prepared.body,
                now="2026-09-24T20:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    ExactJsonTransportResponse(raw),
                )[1],
                sender_check=lambda _owner, _epoch: None,
                submission_scope={
                    "endpoint": prepared.endpoint,
                    "prepared_request_sha256": prepared.body_sha256,
                    "capability_snapshot_ids": list(
                        prepared.capability_snapshot_ids
                    ),
                    "instrument_versions": list(prepared.instrument_versions),
                },
            )
            self.assertEqual(outcome.status, "SENT")
            binding = load_submission_response_binding(
                store,
                environment="PAPER",
                account_id="paper-account",
                attempt_id=attempt,
            )
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="ALPACA",
                endpoint=prepared.endpoint,
                prepared_request_sha256=prepared.body_sha256,
                capability_snapshot_ids=prepared.capability_snapshot_ids,
                instrument_versions=prepared.instrument_versions,
            )
        return attempt, prepared, observation

    def test_success_order_response_is_ack_only_not_fill(self):
        order_id = str(uuid4())
        attempt, prepared, observation = self._durable_submission_observation(
            payload={
                "id": order_id,
                "client_order_id": stable_client_order_id(
                    "ALPACA",
                    "alpaca-submission-intent",
                    environment="PAPER",
                    account_id="paper-account",
                ),
                "status": "filled",
                "filled_qty": "1",
            }
        )
        result = parse_submission_response(
            attempt_id=attempt,
            prepared_request=prepared,
            observation=observation,
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertEqual(result["provider_order_id"], order_id)
        self.assertEqual(
            result["evidence"][0]["sha256"],
            observation.response_sha256,
        )

    def test_submission_response_rejects_attempt_and_scope_relabelling(self):
        order_id = str(uuid4())
        attempt, prepared, observation = self._durable_submission_observation(
            payload={
                "id": order_id,
                "client_order_id": stable_client_order_id(
                    "ALPACA",
                    "alpaca-submission-intent",
                    environment="PAPER",
                    account_id="paper-account",
                ),
            }
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "attempt_id mismatch"):
            parse_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=prepared,
                observation=observation,
            )
        other_intent = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="LIMIT",
            time_in_force="DAY",
            quantity="1",
            limit_price="200",
        )
        wrong_request = prepare_order_request(
            other_intent,
            client_order_id=prepared.body["client_order_id"],
            account_id="paper-account",
            environment="PAPER",
            capability=capability(),
            at=NOW,
        )
        with self.assertRaisesRegex(Exception, "provenance|scope|digest"):
            parse_submission_response(
                attempt_id=attempt,
                prepared_request=wrong_request,
                observation=observation,
            )

    def test_transport_ambiguity_is_unknown_and_reconcile_first(self):
        client_id = stable_client_order_id(
            "ALPACA",
            "alpaca-unknown-intent",
            environment="PAPER",
            account_id="paper-account",
        )
        intent = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        prepared = prepare_order_request(
            intent,
            client_order_id=client_id,
            account_id="paper-account",
            environment="PAPER",
            capability=capability(),
            at=NOW,
        )
        result = parse_submission_response(
            attempt_id=str(uuid4()),
            prepared_request=prepared,
            observation=None,
            transport_ambiguous=True,
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")
        self.assertNotIn("provider_received_at", result)
        self.assertNotIn("observed_at", result)
        self.assertNotIn("provider_environment", result)
        self.assertEqual(result["evidence"], [])

    def test_transport_ambiguity_cannot_claim_provider_response(self):
        _attempt, prepared, observation = self._durable_submission_observation(
            payload={
                "id": str(uuid4()),
                "client_order_id": stable_client_order_id(
                    "ALPACA",
                    "alpaca-submission-intent",
                    environment="PAPER",
                    account_id="paper-account",
                ),
            }
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "must not fabricate"):
            parse_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=prepared,
                observation=observation,
                transport_ambiguous=True,
            )

    def test_decoded_mapping_cannot_mint_submission_authority(self):
        client_id = stable_client_order_id(
            "ALPACA",
            "alpaca-mapping-intent",
            environment="PAPER",
            account_id="paper-account",
        )
        intent = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        prepared = prepare_order_request(
            intent,
            client_order_id=client_id,
            account_id="paper-account",
            environment="PAPER",
            capability=capability(),
            at=NOW,
        )
        with self.assertRaisesRegex(
            TypeError,
            "durable ProviderSubmissionObservation",
        ):
            parse_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=prepared,
                observation={
                    "id": str(uuid4()),
                    "client_order_id": client_id,
                },
            )

    def test_trade_activity_requires_order_and_fee_evidence(self):
        order_id = str(uuid4())
        row = {
            "activity_type": "FILL",
            "id": "20190524113406977::fill-1",
            "order_id": order_id,
            "symbol": "AAPL",
            "side": "buy",
            "qty": "1",
            "price": "220.10",
            "transaction_time": "2026-09-24T20:01:00Z",
        }
        observation = bound_activity_response([row])
        with self.assertRaisesRegex(AlpacaAdapterError, "fee evidence"):
            parse_trade_activities(
                observation,
                instrument_versions={"AAPL": "AAPL:v1"},
                client_ids_by_order_id={order_id: "at-ack-1"},
                fees_by_activity_id={},
            )
        fills = parse_trade_activities(
            bound_activity_response([row, row]),
            instrument_versions={"AAPL": "AAPL:v1"},
            client_ids_by_order_id={order_id: "at-ack-1"},
            fees_by_activity_id={row["id"]: ("0.01", "USD")},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].fee_amount, Decimal("0.01"))
        self.assertEqual((fills[0].account_id, fills[0].environment), ("paper-1", "PAPER"))
        self.assertEqual((fills[0].side, fills[0].position_side), ("BUY", "BOTH"))
        self.assertEqual(fills[0].evidence_refs, (observation.evidence_ref,))

    def test_trade_activity_requires_provider_evidenced_side(self):
        order_id = str(uuid4())
        base = {
            "activity_type": "FILL",
            "id": "activity-side-1",
            "order_id": order_id,
            "symbol": "AAPL",
            "qty": "1",
            "price": "220.10",
            "transaction_time": "2026-09-24T20:01:00Z",
        }
        with self.assertRaisesRegex(AlpacaAdapterError, "activity.side"):
            parse_trade_activities(
                bound_activity_response([base]),
                instrument_versions={"AAPL": "AAPL:v1"},
                client_ids_by_order_id={order_id: "at-side-1"},
                fees_by_activity_id={base["id"]: ("0.01", "USD")},
            )

        invalid = dict(base, side="hold")
        with self.assertRaisesRegex(AlpacaAdapterError, "activity side"):
            parse_trade_activities(
                bound_activity_response([invalid]),
                instrument_versions={"AAPL": "AAPL:v1"},
                client_ids_by_order_id={order_id: "at-side-1"},
                fees_by_activity_id={base["id"]: ("0.01", "USD")},
            )

        sell = dict(base, side="sell")
        sell_observation = bound_activity_response([sell])
        fills = parse_trade_activities(
            sell_observation,
            instrument_versions={"AAPL": "AAPL:v1"},
            client_ids_by_order_id={order_id: "at-side-1"},
            fees_by_activity_id={base["id"]: ("0.01", "USD")},
        )
        self.assertEqual((fills[0].side, fills[0].position_side), ("SELL", "BOTH"))
        self.assertEqual(fills[0].evidence_refs, (sell_observation.evidence_ref,))

    def test_trade_activity_scope_cannot_be_relabelled_after_provider_read(self):
        order_id = str(uuid4())
        row = {
            "activity_type": "FILL",
            "id": "activity-scope-1",
            "order_id": order_id,
            "symbol": "AAPL",
            "side": "buy",
            "qty": "1",
            "price": "220.10",
            "transaction_time": "2026-09-24T20:01:00Z",
        }
        observation = bound_activity_response([row], account_id="account-a")
        fills = parse_trade_activities(
            observation,
            instrument_versions={"AAPL": "AAPL:v1"},
            client_ids_by_order_id={order_id: "at-scope-1"},
            fees_by_activity_id={row["id"]: ("0.01", "USD")},
        )
        self.assertEqual((fills[0].account_id, fills[0].environment), ("account-a", "PAPER"))
        self.assertEqual(observation.query_binding.account_id, "account-a")

    def test_canonical_coverage_defaults_to_unproven_absence(self):
        evidence = coverage_evidence(
            surface="ACTIVITIES",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        
            account_id="paper-1",
            environment="PAPER",)
        self.assertFalse(evidence.provider_semantics_exclude_execution)

    def test_paper_is_not_live_execution_realism_proof(self):
        self.assertFalse(paper_evidence_proves_live_execution_realism())


from mvp.autotrade_mvp.alpaca import (
    AlpacaMlegLeg,
    AlpacaMlegOrderIntent,
    prepare_mleg_order_request,
)


def mleg_capability(instrument_version, *, account_id="paper-account", environment="PAPER"):
    return capability(
        order_types=("MARKET", "LIMIT"),
        tif=("DAY",),
        account_id=account_id,
        environment=environment,
        instrument_version=instrument_version,
    )


class AlpacaMlegFoundationTests(unittest.TestCase):
    def leg(self, symbol, ratio, side, position_intent):
        return AlpacaMlegLeg.create(
            instrument_version=f"{symbol}:v1",
            underlying_version="AAPL:v1",
            symbol=symbol,
            ratio_quantity=ratio,
            side=side,
            position_intent=position_intent,
        )

    def test_limit_spread_keeps_parent_and_leg_identity(self):
        first = self.leg(
            "AAPL261218C00200000", "1", "BUY", "buy_to_open"
        )
        second = self.leg(
            "AAPL261218C00210000", "1", "SELL", "sell_to_open"
        )
        intent = AlpacaMlegOrderIntent.create(
            underlying_version="AAPL:v1",
            quantity="2",
            order_type="LIMIT",
            time_in_force="DAY",
            limit_price="-0.60",
            legs=(first, second),
        )
        request = prepare_mleg_order_request(
            intent,
            client_order_id="at-mleg-1",
            capabilities={
                first.instrument_version: mleg_capability(first.instrument_version),
                second.instrument_version: mleg_capability(second.instrument_version),
            },
            at=NOW,
        )
        self.assertEqual(request.body["order_class"], "mleg")
        self.assertEqual(request.account_id, "paper-account")
        self.assertEqual(request.environment, "PAPER")
        self.assertEqual(
            request.instrument_versions,
            (first.instrument_version, second.instrument_version),
        )
        self.assertEqual(len(request.capability_snapshot_ids), 2)
        self.assertRegex(request.body_sha256, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(request.body["qty"], "2")
        self.assertEqual(request.body["limit_price"], "-0.60")
        self.assertNotIn("symbol", request.body)
        self.assertNotIn("side", request.body)
        self.assertEqual(request.body["legs"][0]["ratio_qty"], "1")
        self.assertEqual(
            request.body["legs"][1]["position_intent"], "sell_to_open"
        )

    def test_market_has_no_parent_limit_price(self):
        first = self.leg(
            "AAPL261218P00200000", 1, "BUY", "buy_to_open"
        )
        second = self.leg(
            "AAPL261218P00190000", 1, "SELL", "sell_to_open"
        )
        intent = AlpacaMlegOrderIntent.create(
            underlying_version="AAPL:v1",
            quantity=1,
            order_type="MARKET",
            time_in_force="DAY",
            legs=(first, second),
        )
        request = prepare_mleg_order_request(
            intent,
            client_order_id="at-mleg-market",
            capabilities={
                first.instrument_version: mleg_capability(first.instrument_version),
                second.instrument_version: mleg_capability(second.instrument_version),
            },
            at=NOW,
        )
        self.assertNotIn("limit_price", request.body)

    def test_mleg_requires_two_to_four_legs(self):
        first = self.leg(
            "AAPL261218C00200000", 1, "BUY", "buy_to_open"
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "2 to 4"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="MARKET",
                time_in_force="DAY",
                legs=(first,),
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "2 to 4"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="MARKET",
                time_in_force="DAY",
                legs=(first, first, first, first, first),
            )

    def test_duplicate_leg_identity_must_use_ratio_quantity(self):
        first = self.leg(
            "AAPL261218C00200000", 1, "BUY", "buy_to_open"
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "instrument versions"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="DAY",
                limit_price="1",
                legs=(first, first),
            )

        duplicate_symbol = AlpacaMlegLeg.create(
            instrument_version="AAPL261218C00200000:v2",
            underlying_version="AAPL:v1",
            symbol="AAPL261218C00200000",
            ratio_quantity=1,
            side="SELL",
            position_intent="sell_to_open",
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "provider symbols"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="DAY",
                limit_price="1",
                legs=(first, duplicate_symbol),
            )

    def test_ratio_quantities_must_be_whole_positive_and_reduced(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "whole"):
            self.leg("AAPL261218C00200000", "1.5", "BUY", "buy_to_open")
        first = self.leg(
            "AAPL261218C00200000", 4, "BUY", "buy_to_open"
        )
        second = self.leg(
            "AAPL261218C00210000", 2, "SELL", "sell_to_open"
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "simplest"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="DAY",
                limit_price="1",
                legs=(first, second),
            )

    def test_position_intent_must_match_leg_side(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "BUY"):
            self.leg(
                "AAPL261218C00200000",
                1,
                "SELL",
                "buy_to_open",
            )

    def test_underlying_mismatch_fails_closed(self):
        first = self.leg(
            "AAPL261218C00200000", 1, "BUY", "buy_to_open"
        )
        second = AlpacaMlegLeg.create(
            instrument_version="MSFT261218C00500000:v1",
            underlying_version="MSFT:v1",
            symbol="MSFT261218C00500000",
            ratio_quantity=1,
            side="SELL",
            position_intent="sell_to_open",
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "underlying"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="DAY",
                limit_price="1",
                legs=(first, second),
            )

    def test_limit_credit_and_debit_are_exact_but_zero_is_rejected(self):
        first = self.leg(
            "AAPL261218C00200000", 1, "BUY", "buy_to_open"
        )
        second = self.leg(
            "AAPL261218C00210000", 1, "SELL", "sell_to_open"
        )
        credit = AlpacaMlegOrderIntent.create(
            underlying_version="AAPL:v1",
            quantity=1,
            order_type="LIMIT",
            time_in_force="DAY",
            limit_price="-1.2500",
            legs=(first, second),
        )
        self.assertEqual(credit.limit_price, Decimal("-1.2500"))
        with self.assertRaisesRegex(AlpacaAdapterError, "non-zero"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="DAY",
                limit_price="0",
                legs=(first, second),
            )
        with self.assertRaises(AlpacaAdapterError):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="DAY",
                limit_price=1.25,
                legs=(first, second),
            )

    def test_day_only_is_explicit_until_mleg_gtc_is_qualified(self):
        first = self.leg(
            "AAPL261218C00200000", 1, "BUY", "buy_to_open"
        )
        second = self.leg(
            "AAPL261218C00210000", 1, "SELL", "sell_to_open"
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "DAY"):
            AlpacaMlegOrderIntent.create(
                underlying_version="AAPL:v1",
                quantity=1,
                order_type="LIMIT",
                time_in_force="GTC",
                limit_price="1",
                legs=(first, second),
            )

    def test_every_leg_needs_same_account_exact_capability(self):
        first = self.leg(
            "AAPL261218C00200000", 1, "BUY", "buy_to_open"
        )
        second = self.leg(
            "AAPL261218C00210000", 1, "SELL", "sell_to_open"
        )
        intent = AlpacaMlegOrderIntent.create(
            underlying_version="AAPL:v1",
            quantity=1,
            order_type="LIMIT",
            time_in_force="DAY",
            limit_price="1",
            legs=(first, second),
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "missing exact capability"):
            prepare_mleg_order_request(
                intent,
                client_order_id="at-mleg-missing",
                capabilities={
                    first.instrument_version: mleg_capability(first.instrument_version)
                },
                at=NOW,
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "same account"):
            prepare_mleg_order_request(
                intent,
                client_order_id="at-mleg-cross-account",
                capabilities={
                    first.instrument_version: mleg_capability(
                        first.instrument_version, account_id="a"
                    ),
                    second.instrument_version: mleg_capability(
                        second.instrument_version, account_id="b"
                    ),
                },
                at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
