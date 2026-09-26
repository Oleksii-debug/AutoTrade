from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

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
    ProviderCoreError,
    Surface,
    observe_authenticated_json_response,
    observe_submission_json_response,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.kraken_spot import (
    KRAKEN_SPOT_DOCS,
    KrakenSpotAbsenceEvidence,
    KrakenSpotAdapterError,
    KrakenSpotOrderIntent,
    KrakenSpotOpenOrdersSnapshotEvidence,
    KrakenSpotPageEvidence,
    KrakenSpotPaginationCoverage,
    absence_evidence_from_pagination,
    KrakenSpotPreparedRequest,
    coverage_evidence,
    derivatives_supported_by_this_module,
    parse_trade_history,
    parse_spot_submission_response,
    open_orders_snapshot_from_observation,
    pagination_page_from_observation,
    prepare_spot_order_request,
    validate_spot_client_order_id,
)


NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)


def trade_history_observation(
    response,
    *,
    account_id="paper-1",
    environment="PAPER",
    surface=Surface.ACTIVITIES,
    query=None,
):
    query = prepare_authenticated_read_query(
        capability=capability(
            account_id=account_id,
            environment=environment,
        ),
        surface=surface,
        endpoint="/0/private/TradesHistory",
        query={"ofs": "0"} if query is None else query,
        at=NOW,
        permission_scope="TRADE.READ",
    )
    raw = json.dumps(
        response,
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

def authenticated_activity_observation(
    endpoint,
    response,
    *,
    query=None,
    permission_scope="ORDER.READ",
    account_id="paper-1",
    environment="PAPER",
):
    binding = prepare_authenticated_read_query(
        capability=capability(
            account_id=account_id,
            environment=environment,
        ),
        surface=Surface.ACTIVITIES,
        endpoint=endpoint,
        query={} if query is None else query,
        at=NOW,
        permission_scope=permission_scope,
    )
    raw = json.dumps(
        response,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return observe_authenticated_json_response(
        query_binding=binding,
        http_status=200,
        response_bytes=raw,
        observed_at=NOW,
    )


def capability(
    *,
    order_types=("MARKET", "LIMIT"),
    tif=("GTC", "IOC"),
    account_id="spot-account",
    environment="PAPER",
):
    observed_at = NOW - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="KRAKEN",
            account_id=account_id,
            entity_id="kraken-spot",
            environment=environment,
            instrument_version="XBTUSD:v1",
            observed_at=observed_at,
            expires_at=NOW + timedelta(hours=1),
            supported_order_types=frozenset(order_types),
            time_in_force=frozenset(tif),
            permission_scopes=frozenset({"ORDER_WRITE", "ORDER.READ", "TRADE.READ", "ACCOUNT.READ"}),
            position_mode="CASH",
            native_protection=frozenset(),
            rate_limit_policy_id="kraken-spot-test",
            data_entitlements=frozenset({"ORDERS", "TRADES", "LEDGERS"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "b" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://www.kraken.com/features/trading-api",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=observed_at,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


class KrakenSpotAdapterTests(unittest.TestCase):
    def test_direct_prepared_request_requires_canonical_factory(self):
        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "canonical preparation factory",
        ):
            KrakenSpotPreparedRequest(
                endpoint="/0/private/AddOrder",
                body={
                    "pair": "XBTUSD",
                    "type": "buy",
                    "ordertype": "market",
                    "volume": "0.01",
                    "cl_ord_id": "at-direct",
                    "timeinforce": "GTC",
                },
                account_id="spot-account",
                environment="LIVE",
                capability_snapshot_id="cap-1",
                documentation_refs=("https://docs.kraken.com/order",),
                instrument_version="XBTUSD:v1",
            )

    def test_limit_request_preserves_exact_decimal_and_has_no_nonce(self):
        intent = KrakenSpotOrderIntent.create(
            instrument_version="XBTUSD:v1",
            pair="xbtusd",
            side="buy",
            order_type="limit",
            volume="0.0100",
            price="60000.25",
        )
        request = prepare_spot_order_request(
            intent,
            client_order_id="at-0123456789abcd",
            account_id="spot-account",
            environment="PAPER",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(request.endpoint, "/0/private/AddOrder")
        self.assertEqual(request.body["volume"], "0.0100")
        self.assertEqual(request.body["price"], "60000.25")
        self.assertEqual(request.body["cl_ord_id"], "at-0123456789abcd")
        self.assertEqual(request.body["timeinforce"], "GTC")
        self.assertNotIn("nonce", request.body)
        self.assertNotIn("deadline", request.body)

    def test_prepared_request_provenance_is_rest_only(self):
        request = prepare_spot_order_request(
            KrakenSpotOrderIntent.create(
                instrument_version="XBTUSD:v1",
                pair="XBTUSD",
                side="BUY",
                order_type="MARKET",
                volume="0.01",
            ),
            client_order_id="at-rest-proof",
            account_id="spot-account",
            environment="PAPER",
            capability=capability(),
            at=NOW,
        )
        self.assertEqual(
            tuple(request.documentation_refs),
            tuple(KRAKEN_SPOT_DOCS.values()),
        )
        self.assertIn(
            "https://docs.kraken.com/api-reference/trading/add-order",
            request.documentation_refs,
        )
        self.assertIn(
            "https://docs.kraken.com/exchange/guides/rest/authentication",
            request.documentation_refs,
        )
        self.assertFalse(
            any("websocket" in ref.lower() for ref in request.documentation_refs)
        )

    def test_dispatcher_style_client_id_must_fit_free_text_limit(self):
        self.assertEqual(validate_spot_client_order_id("at-0123456789abcd"), "at-0123456789abcd")
        with self.assertRaises(KrakenSpotAdapterError):
            validate_spot_client_order_id("at-" + "a" * 30)

    def test_uuid_client_id_is_supported(self):
        value = "6d1b345e-2821-40e2-ad83-4ecb18a06876"
        self.assertEqual(validate_spot_client_order_id(value), value)

    def test_client_order_id_documented_shape_boundaries(self):
        long_uuid = "6d1b345e-2821-40e2-ad83-4ecb18a06876"
        short_uuid = "da8e4ad59b78481c93e589746b0cf91f"
        free_text = "arb-20240509-00010"
        self.assertEqual(validate_spot_client_order_id(long_uuid), long_uuid)
        self.assertEqual(validate_spot_client_order_id(short_uuid), short_uuid)
        self.assertEqual(validate_spot_client_order_id(free_text), free_text)
        self.assertEqual(validate_spot_client_order_id("a" * 18), "a" * 18)
        for invalid in ("a" * 19, "z" * 36, "замовлення"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(KrakenSpotAdapterError):
                    validate_spot_client_order_id(invalid)

    def test_binary_float_money_is_rejected(self):
        with self.assertRaises(KrakenSpotAdapterError):
            KrakenSpotOrderIntent.create(
                instrument_version="XBTUSD:v1",
                pair="XBTUSD",
                side="BUY",
                order_type="LIMIT",
                volume=0.01,
                price="60000",
            )

    def test_post_only_ioc_conflict_fails_before_send(self):
        with self.assertRaisesRegex(KrakenSpotAdapterError, "mutually exclusive"):
            KrakenSpotOrderIntent.create(
                instrument_version="XBTUSD:v1",
                pair="XBTUSD",
                side="BUY",
                order_type="LIMIT",
                volume="0.01",
                price="60000",
                time_in_force="IOC",
                post_only=True,
            )

    def test_capability_mismatch_fails_closed(self):
        intent = KrakenSpotOrderIntent.create(
            instrument_version="XBTUSD:v1",
            pair="XBTUSD",
            side="BUY",
            order_type="LIMIT",
            volume="0.01",
            price="60000",
        )
        with self.assertRaisesRegex(KrakenSpotAdapterError, "capability"):
            prepare_spot_order_request(
                intent,
                client_order_id="at-order-1",
                account_id="spot-account",
                environment="PAPER",
                capability=capability(order_types=("MARKET",)),
                at=NOW,
            )

    def _prepared_submission_request(
        self,
        *,
        account_id="spot-account",
        environment="LIVE",
        intent_id="kraken-spot-submission-intent",
        client_order_id=None,
    ):
        client_id = client_order_id or stable_client_order_id(
            "KRAKEN",
            intent_id,
            environment=environment,
            account_id=account_id,
            max_length=36,
            client_id_format="UUID",
        )
        intent = KrakenSpotOrderIntent.create(
            instrument_version="XBTUSD:v1",
            pair="XBTUSD",
            side="BUY",
            order_type="MARKET",
            volume="0.01",
        )
        return prepare_spot_order_request(
            intent,
            client_order_id=client_id,
            account_id=account_id,
            environment=environment,
            capability=capability(
                account_id=account_id,
                environment=environment,
            ),
            at=NOW,
        )

    def _durable_submission_observation(
        self,
        payload,
        *,
        prepared_request=None,
        intent_id="kraken-spot-submission-intent",
        attempt_id=None,
    ):
        prepared = prepared_request or self._prepared_submission_request(
            intent_id=intent_id,
        )
        attempt = attempt_id or str(uuid4())
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
                environment=prepared.environment,
                account_id=prepared.account_id,
                owner_token="owner",
            )
            outcome = dispatcher.dispatch(
                attempt_id=attempt,
                intent_id=intent_id,
                intent_hash="kraken-spot-intent-hash",
                provider="KRAKEN",
                request=prepared.body,
                now="2026-09-24T20:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    ExactJsonTransportResponse(raw),
                )[1],
                client_id_max_length=36,
                client_id_format="UUID",
                sender_check=lambda _owner, _epoch: None,
                submission_scope={
                    "endpoint": prepared.endpoint,
                    "prepared_request_sha256": prepared.body_sha256,
                    "capability_snapshot_ids": [
                        prepared.capability_snapshot_id
                    ],
                    "instrument_versions": [
                        prepared.instrument_version
                    ],
                },
            )
            self.assertEqual(outcome.status, "SENT")
            binding = load_submission_response_binding(
                store,
                environment=prepared.environment,
                account_id=prepared.account_id,
                attempt_id=attempt,
            )
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="KRAKEN",
                endpoint=prepared.endpoint,
                prepared_request_sha256=prepared.body_sha256,
                capability_snapshot_ids=(
                    prepared.capability_snapshot_id,
                ),
                instrument_versions=(
                    prepared.instrument_version,
                ),
            )
        return attempt, prepared, observation

    def _parse_submission(self, payload, **overrides):
        intent_id = overrides.pop(
            "intent_id",
            "kraken-spot-submission-intent",
        )
        prepared = overrides.pop("prepared_request", None)
        if prepared is None:
            prepared = self._prepared_submission_request(
                intent_id=intent_id,
            )
        attempt_id = overrides.pop("attempt_id", str(uuid4()))
        observation = None
        if payload is not None:
            attempt_id, prepared, observation = self._durable_submission_observation(
                payload,
                prepared_request=prepared,
                intent_id=intent_id,
                attempt_id=attempt_id,
            )
        values = {
            "attempt_id": attempt_id,
            "prepared_request": prepared,
            "source_uri": "https://api.kraken.com/0/private/AddOrder",
            "observation": observation,
        }
        values.update(overrides)
        return parse_spot_submission_response(**values)

    def test_add_order_acknowledgement_never_proves_fill(self):
        result = self._parse_submission(
            {
                "error": [],
                "result": {
                    "descr": {"order": "buy 0.01000000 XBTUSD @ limit 60000.0"},
                    "txid": ["OABC-D123-E456"],
                },
            }
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["provider_order_id"], "OABC-D123-E456")
        self.assertEqual(result["retry_disposition"], "NEVER")
        self.assertNotIn("fill", repr(result).lower())
        self.assertEqual(
            result["evidence"][0]["source_uri"],
            "https://api.kraken.com/0/private/AddOrder",
        )
        self.assertNotIn("provider_environment", result["evidence"][0])

    def test_provider_error_is_canonical_rejection_not_exception_or_success(self):
        result = self._parse_submission(
            {"error": ["EOrder:Insufficient funds"], "result": None}
        )
        self.assertEqual(result["outcome"], "REJECTED")
        self.assertEqual(result["retry_disposition"], "NEVER")

    def test_deadline_elapsed_is_unknown_after_durable_response_binding(self):
        result = self._parse_submission(
            {"error": ["EService:Deadline elapsed"], "result": None}
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(
            result["reason_code"],
            "KRAKEN_SPOT_DEADLINE_ELAPSED_AMBIGUOUS",
        )
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")
        self.assertEqual(len(result["evidence"]), 1)
        self.assertTrue(result["evidence"][0]["sha256"].startswith("sha256:"))

    def test_transport_ambiguity_is_unknown_and_reconcile_first(self):
        result = self._parse_submission(
            None,
            transport_ambiguous=True,
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")
        self.assertEqual(result["evidence"], [])
        self.assertNotIn("provider_received_at", result)
        self.assertNotIn("observed_at", result)
        self.assertNotIn("provider_environment", result)

        with self.assertRaisesRegex(KrakenSpotAdapterError, "must not fabricate"):
            self._parse_submission(
                {"error": [], "result": {"txid": ["OABC-D123-E456"]}},
                transport_ambiguous=True,
            )

    def test_submission_scope_requires_live_prepared_request_even_when_unknown(self):
        for payload, ambiguous in (
            ({"error": [], "result": {"txid": ["OABC-D123-E456"]}}, False),
            (None, True),
        ):
            with self.subTest(transport_ambiguous=ambiguous):
                with self.assertRaisesRegex(KrakenSpotAdapterError, "qualified only for LIVE"):
                    self._parse_submission(
                        payload,
                        prepared_request=self._prepared_submission_request(
                            environment="PAPER"
                        ),
                        transport_ambiguous=ambiguous,
                    )

    def test_submission_provenance_requires_exact_https_add_order_endpoint(self):
        with self.assertRaisesRegex(KrakenSpotAdapterError, "source_uri"):
            self._parse_submission(
                {"error": [], "result": {"txid": ["OABC-D123-E456"]}},
                source_uri="http://example.invalid/0/private/AddOrder",
            )
        with self.assertRaisesRegex(KrakenSpotAdapterError, "source_uri"):
            self._parse_submission(
                {"error": [], "result": {"txid": ["OABC-D123-E456"]}},
                source_uri="https://example.invalid/0/private/AddOrder",
            )

    def test_submission_evidence_binds_exact_prepared_account_and_capability(self):
        payload = {"error": [], "result": {"txid": ["OABC-D123-E456"]}}
        first = self._parse_submission(
            payload,
            prepared_request=self._prepared_submission_request(
                account_id="spot-account-a",
            ),
        )
        second = self._parse_submission(
            payload,
            prepared_request=self._prepared_submission_request(
                account_id="spot-account-b",
            ),
        )
        self.assertEqual(
            first["evidence"][0]["sha256"],
            second["evidence"][0]["sha256"],
        )
        self.assertNotEqual(
            first["evidence"][0]["artifact_id"],
            second["evidence"][0]["artifact_id"],
        )

    def test_submission_response_rejects_attempt_and_scope_relabelling(self):
        attempt, prepared, observation = self._durable_submission_observation(
            {"error": [], "result": {"txid": ["OABC-D123-E456"]}}
        )
        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "attempt_id mismatch",
        ):
            parse_spot_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=prepared,
                source_uri="https://api.kraken.com/0/private/AddOrder",
                observation=observation,
            )
        wrong_request = self._prepared_submission_request(
            account_id="other-spot-account",
        )
        with self.assertRaisesRegex(Exception, "provenance|digest|account|scope"):
            parse_spot_submission_response(
                attempt_id=attempt,
                prepared_request=wrong_request,
                source_uri="https://api.kraken.com/0/private/AddOrder",
                observation=observation,
            )

    def test_decoded_mapping_cannot_mint_submission_authority(self):
        prepared = self._prepared_submission_request()
        with self.assertRaisesRegex(
            TypeError,
            "durable ProviderSubmissionObservation",
        ):
            parse_spot_submission_response(
                attempt_id=str(uuid4()),
                prepared_request=prepared,
                source_uri="https://api.kraken.com/0/private/AddOrder",
                observation={"error": [], "result": {"txid": ["forged"]}},
            )

    def test_empty_txid_fails_closed(self):
        with self.assertRaisesRegex(KrakenSpotAdapterError, "transaction id"):
            self._parse_submission({"error": [], "result": {"txid": []}})

    def test_multiple_provider_order_ids_fail_closed_until_contract_supports_them(self):
        with self.assertRaisesRegex(KrakenSpotAdapterError, "exactly one"):
            self._parse_submission(
                {"error": [], "result": {"txid": ["OABC-D123-E456", "OABC-D123-E457"]}}
            )

    def test_incomplete_search_never_proves_absence(self):
        evidence = KrakenSpotAbsenceEvidence(
            order_found=False,
            open_orders_complete=True,
            closed_orders_complete=True,
            trades_complete=False,
            ledgers_complete=False,
            consistency_horizon_satisfied=True,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

    def test_complete_surfaces_do_not_self_qualify_provider_absence(self):
        evidence = KrakenSpotAbsenceEvidence(
            order_found=False,
            open_orders_complete=True,
            closed_orders_complete=True,
            trades_complete=True,
            ledgers_complete=True,
            consistency_horizon_satisfied=True,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")
        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "cannot self-assert provider exclusion semantics",
        ):
            KrakenSpotAbsenceEvidence(
                order_found=False,
                open_orders_complete=True,
                closed_orders_complete=True,
                trades_complete=True,
                ledgers_complete=True,
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
            )

    def test_derivatives_are_not_silently_claimed_by_spot_module(self):
        self.assertFalse(derivatives_supported_by_this_module())


    def test_trade_history_uses_trade_id_as_unique_fill_identity(self):
        response = {
            "error": [],
            "result": {
                "count": 1,
                "trades": {
                    "T-EXEC-1": {
                        "ordertxid": "OABC-D123-E456",
                        "pair": "XXBTZUSD",
                        "type": "buy",
                        "time": "1790280001.123456",
                        "price": "60000.25",
                        "vol": "0.0100",
                        "fee": "0.20",
                    }
                },
            },
        }
        fills = parse_trade_history(
            trade_history_observation(response),
            instrument_versions={"XXBTZUSD": "XBTUSD:v1"},
            client_ids_by_provider_order={"OABC-D123-E456": "at-order-1"},
            fee_currency_by_pair={"XXBTZUSD": "USD"},
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertEqual(fill.provider_execution_id, "T-EXEC-1")
        self.assertEqual(fill.client_order_id, "at-order-1")
        self.assertEqual(fill.instrument, "XBTUSD:v1")
        self.assertEqual(fill.side, "BUY")
        self.assertEqual(fill.quantity, Decimal("0.0100"))
        self.assertEqual(fill.price, Decimal("60000.25"))
        self.assertEqual(fill.fee_amount, Decimal("0.20"))
        self.assertEqual(fill.fee_currency, "USD")
        self.assertEqual(fill.trade_time, "2026-09-24T20:00:01.123456Z")

    def test_trade_history_side_is_provider_evidenced_and_fail_closed(self):
        trade = {
            "ordertxid": "OABC-D123-E456",
            "pair": "XXBTZUSD",
            "type": "sell",
            "time": "1790280001.123456",
            "price": "60000.25",
            "vol": "0.0100",
            "fee": "0.20",
        }
        kwargs = {
            "instrument_versions": {"XXBTZUSD": "XBTUSD:v1"},
            "client_ids_by_provider_order": {
                "OABC-D123-E456": "at-order-1"
            },
            "fee_currency_by_pair": {"XXBTZUSD": "USD"},
        }
        response = {
            "error": [],
            "result": {"trades": {"T-EXEC-SIDE": dict(trade)}},
        }
        fill = parse_trade_history(
            trade_history_observation(response), **kwargs
        )[0]
        self.assertEqual(fill.side, "SELL")

        missing = dict(trade)
        missing.pop("type")
        response["result"]["trades"]["T-EXEC-SIDE"] = missing
        with self.assertRaisesRegex(KrakenSpotAdapterError, "trade.type"):
            parse_trade_history(trade_history_observation(response), **kwargs)

        for invalid in ("BUY", "SELL", "Buy", "Sell", "unknown"):
            with self.subTest(type=invalid):
                response["result"]["trades"]["T-EXEC-SIDE"] = {
                    **trade,
                    "type": invalid,
                }
                with self.assertRaisesRegex(
                    KrakenSpotAdapterError,
                    "provider-evidenced buy or sell",
                ):
                    parse_trade_history(
                        trade_history_observation(response), **kwargs
                    )

    def test_trade_history_never_invents_missing_fee_as_zero(self):
        base_trade = {
            "ordertxid": "OABC-D123-E456",
            "pair": "XXBTZUSD",
            "type": "buy",
            "time": "1790280001.123456",
            "price": "60000.25",
            "vol": "0.0100",
        }
        response = {
            "error": [],
            "result": {"trades": {"T-EXEC-1": dict(base_trade)}},
        }
        kwargs = {
            "instrument_versions": {"XXBTZUSD": "XBTUSD:v1"},
            "client_ids_by_provider_order": {
                "OABC-D123-E456": "at-order-1"
            },
            "fee_currency_by_pair": {"XXBTZUSD": "USD"},
        }
        with self.assertRaisesRegex(KrakenSpotAdapterError, "fee amount"):
            parse_trade_history(trade_history_observation(response), **kwargs)

        response["result"]["trades"]["T-EXEC-1"]["fee"] = "0"
        fills = parse_trade_history(trade_history_observation(response), **kwargs)
        self.assertEqual(fills[0].fee_amount, Decimal("0"))

        response["result"]["trades"]["T-EXEC-1"]["fee"] = "0.25"
        fills = parse_trade_history(trade_history_observation(response), **kwargs)
        self.assertEqual(fills[0].fee_amount, Decimal("0.25"))

    def test_trade_history_refuses_to_guess_instrument_or_fee_currency(self):
        response = {
            "error": [],
            "result": {
                "trades": {
                    "T-EXEC-1": {
                        "ordertxid": "OABC-D123-E456",
                        "pair": "XXBTZUSD",
                        "type": "buy",
                        "time": "1790280001",
                        "price": "60000",
                        "vol": "0.01",
                        "fee": "0.2",
                    }
                }
            },
        }
        with self.assertRaisesRegex(KrakenSpotAdapterError, "unmapped Kraken pair"):
            parse_trade_history(
                trade_history_observation(response),
                instrument_versions={},
                client_ids_by_provider_order={},
                fee_currency_by_pair={"XXBTZUSD": "USD"},
            )
        with self.assertRaisesRegex(KrakenSpotAdapterError, "fee currency"):
            parse_trade_history(
                trade_history_observation(response),
                instrument_versions={"XXBTZUSD": "XBTUSD:v1"},
                client_ids_by_provider_order={},
                fee_currency_by_pair={},
            )

    def test_trade_timestamp_preserves_exact_json_decimal_and_rejects_sub_microsecond_precision(self):
        base = {
            "error": [],
            "result": {
                "trades": {
                    "T-EXEC-1": {
                        "ordertxid": "OABC-D123-E456",
                        "pair": "XXBTZUSD",
                        "type": "buy",
                        "time": 1790280001.25,
                        "price": "60000",
                        "vol": "0.01",
                        "fee": "0.2",
                    }
                }
            },
        }
        fills = parse_trade_history(
            trade_history_observation(base),
            instrument_versions={"XXBTZUSD": "XBTUSD:v1"},
            client_ids_by_provider_order={},
            fee_currency_by_pair={"XXBTZUSD": "USD"},
        )
        self.assertEqual(fills[0].trade_time, "2026-09-24T20:00:01.250000Z")
        base["result"]["trades"]["T-EXEC-1"]["time"] = "1790280001.1234567"
        with self.assertRaisesRegex(KrakenSpotAdapterError, "microsecond"):
            parse_trade_history(
                trade_history_observation(base),
                instrument_versions={"XXBTZUSD": "XBTUSD:v1"},
                client_ids_by_provider_order={},
                fee_currency_by_pair={"XXBTZUSD": "USD"},
            )


    def test_trade_history_scope_comes_only_from_pre_io_observation(self):
        response = {
            "error": [],
            "result": {
                "trades": {
                    "T-SCOPE-1": {
                        "ordertxid": "O-SCOPE-1",
                        "pair": "XXBTZUSD",
                        "type": "buy",
                        "time": "1790280001",
                        "price": "60000",
                        "vol": "0.01",
                        "fee": "0.2",
                    }
                }
            },
        }
        observation = trade_history_observation(
            response,
            account_id="bound-account",
            environment="PAPER",
        )
        fill = parse_trade_history(
            observation,
            instrument_versions={"XXBTZUSD": "XBTUSD:v1"},
            client_ids_by_provider_order={"O-SCOPE-1": "at-order-1"},
            fee_currency_by_pair={"XXBTZUSD": "USD"},
        )[0]
        self.assertEqual(fill.account_id, "bound-account")
        self.assertEqual(fill.environment, "PAPER")
        self.assertEqual(fill.evidence_refs, (observation.evidence_ref,))

        wrong_surface = trade_history_observation(
            response,
            account_id="bound-account",
            environment="PAPER",
            surface=Surface.AUTHENTICATED_READ,
        )
        with self.assertRaisesRegex(ProviderCoreError, "surface mismatch"):
            parse_trade_history(
                wrong_surface,
                instrument_versions={"XXBTZUSD": "XBTUSD:v1"},
                client_ids_by_provider_order={"O-SCOPE-1": "at-order-1"},
                fee_currency_by_pair={"XXBTZUSD": "USD"},
            )

    def test_trade_history_pagination_coverage_binds_exact_pages_and_total(self):
        first_observation = trade_history_observation(
            {
                "error": [],
                "result": {
                    "trades": {"T-1": {}, "T-2": {}},
                    "count": 3,
                },
            },
            query={"ofs": "0", "limit": "2", "type": "all", "end": "1790385000"},
        )
        second_observation = trade_history_observation(
            {
                "error": [],
                "result": {
                    "trades": {"T-3": {}},
                    "count": 3,
                },
            },
            query={"ofs": "2", "limit": "2", "type": "all", "end": "1790385000"},
        )
        coverage = KrakenSpotPaginationCoverage(surface="EXECUTIONS")
        first = pagination_page_from_observation(
            first_observation,
            surface="EXECUTIONS",
        )
        second = pagination_page_from_observation(
            second_observation,
            surface="EXECUTIONS",
        )

        coverage.add_page(first)
        self.assertFalse(coverage.complete)
        self.assertEqual(coverage.next_offset, 2)
        coverage.add_page(second)
        self.assertTrue(coverage.complete)
        self.assertEqual(coverage.next_offset, 3)
        self.assertEqual(
            coverage.evidence_refs,
            (
                first_observation.evidence_ref,
                second_observation.evidence_ref,
            ),
        )

    def test_multi_page_kraken_coverage_without_end_boundary_stays_incomplete(self):
        first = pagination_page_from_observation(
            trade_history_observation(
                {
                    "error": [],
                    "result": {
                        "trades": {"T-1": {}, "T-2": {}},
                        "count": 3,
                    },
                },
                query={"ofs": "0", "limit": "2", "type": "all"},
            ),
            surface="EXECUTIONS",
        )
        second = pagination_page_from_observation(
            trade_history_observation(
                {
                    "error": [],
                    "result": {
                        "trades": {"T-3": {}},
                        "count": 3,
                    },
                },
                query={"ofs": "2", "limit": "2", "type": "all"},
            ),
            surface="EXECUTIONS",
        )
        coverage = KrakenSpotPaginationCoverage(surface="EXECUTIONS")
        coverage.add_page(first)
        coverage.add_page(second)

        self.assertFalse(coverage.has_stable_end_boundary)
        self.assertFalse(coverage.complete)
        self.assertEqual(coverage.next_offset, 3)

    def test_kraken_pagination_rejects_record_overlap_across_pages(self):
        first = pagination_page_from_observation(
            trade_history_observation(
                {
                    "error": [],
                    "result": {
                        "trades": {"T-1": {}, "T-2": {}},
                        "count": 4,
                    },
                },
                query={
                    "ofs": "0",
                    "limit": "2",
                    "type": "all",
                    "end": "1790385000",
                },
            ),
            surface="EXECUTIONS",
        )
        second = pagination_page_from_observation(
            trade_history_observation(
                {
                    "error": [],
                    "result": {
                        "trades": {"T-2": {}, "T-3": {}},
                        "count": 4,
                    },
                },
                query={
                    "ofs": "2",
                    "limit": "2",
                    "type": "all",
                    "end": "1790385000",
                },
            ),
            surface="EXECUTIONS",
        )
        coverage = KrakenSpotPaginationCoverage(surface="EXECUTIONS")
        coverage.add_page(first)

        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "duplicated across pages",
        ):
            coverage.add_page(second)

        self.assertEqual(coverage.pages, (first,))
        self.assertFalse(coverage.complete)

    def test_multi_page_kraken_coverage_rejects_pseudo_end_boundaries(self):
        for end in ("", "not a boundary", "001", "opaque"):
            with self.subTest(end=end):
                first = pagination_page_from_observation(
                    trade_history_observation(
                        {
                            "error": [],
                            "result": {
                                "trades": {"T-1": {}, "T-2": {}},
                                "count": 3,
                            },
                        },
                        query={
                            "ofs": "0",
                            "limit": "2",
                            "type": "all",
                            "end": end,
                        },
                    ),
                    surface="EXECUTIONS",
                )
                second = pagination_page_from_observation(
                    trade_history_observation(
                        {
                            "error": [],
                            "result": {
                                "trades": {"T-3": {}},
                                "count": 3,
                            },
                        },
                        query={
                            "ofs": "2",
                            "limit": "2",
                            "type": "all",
                            "end": end,
                        },
                    ),
                    surface="EXECUTIONS",
                )
                coverage = KrakenSpotPaginationCoverage(surface="EXECUTIONS")
                coverage.add_page(first)
                coverage.add_page(second)

                self.assertFalse(coverage.has_stable_end_boundary)
                self.assertFalse(coverage.complete)

    def test_kraken_pagination_rejects_gap_filter_or_total_drift(self):
        first = pagination_page_from_observation(
            trade_history_observation(
                {
                    "error": [],
                    "result": {
                        "trades": {"T-1": {}, "T-2": {}},
                        "count": 4,
                    },
                },
                query={"ofs": "0", "limit": "2", "type": "all"},
            ),
            surface="EXECUTIONS",
        )
        cases = (
            (
                {"ofs": "1", "limit": "2", "type": "all"},
                {"trades": {"T-3": {}, "T-4": {}}, "count": 4},
                "pagination gap",
            ),
            (
                {"ofs": "2", "limit": "2", "type": "closed position"},
                {"trades": {"T-3": {}, "T-4": {}}, "count": 4},
                "filters changed",
            ),
            (
                {"ofs": "2", "limit": "2", "type": "all"},
                {"trades": {"T-3": {}, "T-4": {}}, "count": 5},
                "total count changed",
            ),
        )
        for query, result, message in cases:
            with self.subTest(message=message):
                coverage = KrakenSpotPaginationCoverage(surface="EXECUTIONS")
                coverage.add_page(first)
                page = pagination_page_from_observation(
                    trade_history_observation(
                        {"error": [], "result": result},
                        query=query,
                    ),
                    surface="EXECUTIONS",
                )
                with self.assertRaisesRegex(KrakenSpotAdapterError, message):
                    coverage.add_page(page)

    def test_kraken_pagination_rejects_incomplete_or_unbound_evidence(self):
        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "short page",
        ):
            pagination_page_from_observation(
                trade_history_observation(
                    {
                        "error": [],
                        "result": {
                            "trades": {"T-1": {}},
                            "count": 3,
                        },
                    },
                    query={"ofs": "0", "limit": "2"},
                ),
                surface="EXECUTIONS",
            )

        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "without_count",
        ):
            pagination_page_from_observation(
                trade_history_observation(
                    {
                        "error": [],
                        "result": {
                            "trades": {},
                            "count": 0,
                        },
                    },
                    query={
                        "ofs": "0",
                        "limit": "50",
                        "without_count": "true",
                    },
                ),
                surface="EXECUTIONS",
            )

        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "exact response observation",
        ):
            KrakenSpotPageEvidence(
                surface="EXECUTIONS",
                account_id="paper-1",
                environment="PAPER",
                offset=0,
                limit=50,
                record_count=0,
                record_ids=(),
                total_count=0,
                evidence_ref="provider-read:sha256:" + "0" * 64,
                filter_items=(),
            )

    def test_open_orders_snapshot_requires_unfiltered_exact_observation(self):
        observation = authenticated_activity_observation(
            "/0/private/OpenOrders",
            {
                "error": [],
                "result": {"open": {"O-2": {}, "O-1": {}}},
            },
            query={"trades": "true"},
        )
        snapshot = open_orders_snapshot_from_observation(observation)
        self.assertEqual(snapshot.account_id, "paper-1")
        self.assertEqual(snapshot.environment, "PAPER")
        self.assertEqual(snapshot.order_ids, ("O-1", "O-2"))
        self.assertTrue(snapshot.complete_for_account)
        self.assertEqual(snapshot.evidence_ref, observation.evidence_ref)

        for query in (
            {"userref": "1"},
            {"cl_ord_id": "client-1"},
        ):
            with self.subTest(query=query), self.assertRaisesRegex(
                KrakenSpotAdapterError,
                "cannot prove account-wide completeness",
            ):
                open_orders_snapshot_from_observation(
                    authenticated_activity_observation(
                        "/0/private/OpenOrders",
                        {"error": [], "result": {"open": {}}},
                        query=query,
                    )
                )

        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "exact response observation",
        ):
            KrakenSpotOpenOrdersSnapshotEvidence(
                account_id="paper-1",
                environment="PAPER",
                evidence_ref="provider-read:sha256:" + "0" * 64,
                order_ids=(),
                filter_items=(),
            )

    def test_kraken_pagination_rejects_cross_account_scope(self):
        first = pagination_page_from_observation(
            trade_history_observation(
                {
                    "error": [],
                    "result": {
                        "trades": {"T-1": {}},
                        "count": 2,
                    },
                },
                account_id="paper-1",
                query={"ofs": "0", "limit": "1"},
            ),
            surface="EXECUTIONS",
        )
        second = pagination_page_from_observation(
            trade_history_observation(
                {
                    "error": [],
                    "result": {
                        "trades": {"T-2": {}},
                        "count": 2,
                    },
                },
                account_id="paper-2",
                query={"ofs": "1", "limit": "1"},
            ),
            surface="EXECUTIONS",
        )
        coverage = KrakenSpotPaginationCoverage(surface="EXECUTIONS")
        coverage.add_page(first)
        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "account/environment scope changed",
        ):
            coverage.add_page(second)

    def test_absence_evidence_consumes_concrete_kraken_pagination_coverages(self):
        order_history = KrakenSpotPaginationCoverage(surface="ORDER_HISTORY")
        order_history.add_page(
            pagination_page_from_observation(
                authenticated_activity_observation(
                    "/0/private/ClosedOrders",
                    {
                        "error": [],
                        "result": {"closed": {}, "count": 0},
                    },
                ),
                surface="ORDER_HISTORY",
            )
        )
        executions = KrakenSpotPaginationCoverage(surface="EXECUTIONS")
        executions.add_page(
            pagination_page_from_observation(
                trade_history_observation(
                    {
                        "error": [],
                        "result": {"trades": {}, "count": 0},
                    },
                    query={"ofs": "0", "limit": "50"},
                ),
                surface="EXECUTIONS",
            )
        )
        activities = KrakenSpotPaginationCoverage(surface="ACTIVITIES")
        activities.add_page(
            pagination_page_from_observation(
                authenticated_activity_observation(
                    "/0/private/Ledgers",
                    {
                        "error": [],
                        "result": {"ledger": {}, "count": 0},
                    },
                    permission_scope="ACCOUNT.READ",
                ),
                surface="ACTIVITIES",
            )
        )

        open_orders = open_orders_snapshot_from_observation(
            authenticated_activity_observation(
                "/0/private/OpenOrders",
                {
                    "error": [],
                    "result": {"open": {}},
                },
            )
        )
        evidence = absence_evidence_from_pagination(
            order_found=False,
            open_orders=open_orders,
            order_history=order_history,
            executions=executions,
            activities=activities,
            consistency_horizon_satisfied=True,
        )
        self.assertTrue(evidence.open_orders_complete)
        self.assertTrue(evidence.closed_orders_complete)
        self.assertTrue(evidence.trades_complete)
        self.assertTrue(evidence.ledgers_complete)
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "cannot self-assert provider exclusion semantics",
        ):
            absence_evidence_from_pagination(
                order_found=False,
                open_orders=open_orders,
                order_history=order_history,
                executions=executions,
                activities=activities,
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
            )

    def test_filtered_history_cannot_be_consumed_as_account_wide_absence(self):
        specs = {
            "ORDER_HISTORY": (
                "/0/private/ClosedOrders",
                "closed",
                "ORDER.READ",
                {"userref": "17"},
            ),
            "EXECUTIONS": (
                "/0/private/TradesHistory",
                "trades",
                "TRADE.READ",
                {"pair": "XBT/USD", "limit": "50"},
            ),
            "ACTIVITIES": (
                "/0/private/Ledgers",
                "ledger",
                "ACCOUNT.READ",
                {"asset": "USD"},
            ),
        }

        def coverage_for(surface, *, query=None):
            endpoint, records_key, permission_scope, default_query = specs[surface]
            observation = authenticated_activity_observation(
                endpoint,
                {
                    "error": [],
                    "result": {records_key: {}, "count": 0},
                },
                query=default_query if query is None else query,
                permission_scope=permission_scope,
            )
            coverage = KrakenSpotPaginationCoverage(surface=surface)
            coverage.add_page(
                pagination_page_from_observation(
                    observation,
                    surface=surface,
                )
            )
            return coverage

        filtered = {
            surface: coverage_for(surface)
            for surface in specs
        }
        for surface, coverage in filtered.items():
            with self.subTest(surface=surface):
                self.assertTrue(coverage.complete)
                self.assertFalse(coverage.complete_for_account)

        end_bounded_executions = coverage_for(
            "EXECUTIONS",
            query={"end": "1790385000", "limit": "50"},
        )
        self.assertTrue(end_bounded_executions.complete_for_account)

        unfiltered = {
            "ORDER_HISTORY": coverage_for("ORDER_HISTORY", query={}),
            "EXECUTIONS": coverage_for(
                "EXECUTIONS",
                query={"limit": "50"},
            ),
            "ACTIVITIES": coverage_for("ACTIVITIES", query={}),
        }
        open_orders = open_orders_snapshot_from_observation(
            authenticated_activity_observation(
                "/0/private/OpenOrders",
                {
                    "error": [],
                    "result": {"open": {}},
                },
            )
        )

        for surface in specs:
            coverages = dict(unfiltered)
            coverages[surface] = filtered[surface]
            with self.subTest(rejected_surface=surface):
                with self.assertRaisesRegex(
                    KrakenSpotAdapterError,
                    "does not cover account-wide population",
                ):
                    absence_evidence_from_pagination(
                        order_found=False,
                        open_orders=open_orders,
                        order_history=coverages["ORDER_HISTORY"],
                        executions=coverages["EXECUTIONS"],
                        activities=coverages["ACTIVITIES"],
                        consistency_horizon_satisfied=True,
                    )

    def test_coverage_defaults_to_non_authoritative_absence(self):
        coverage = coverage_evidence(
            surface="executions",
            coverage_start="2026-09-24T19:00:00Z",
            coverage_end="2026-09-24T21:00:00Z",
            pagination_complete=True,
            consistency_horizon_satisfied=True,
        
            account_id="paper-1",
            environment="PAPER",)
        self.assertEqual(coverage.surface, "EXECUTIONS")
        self.assertFalse(coverage.provider_semantics_exclude_execution)
        self.assertFalse(coverage.proves_absence_for(NOW))
        with self.assertRaisesRegex(
            KrakenSpotAdapterError,
            "cannot self-assert provider exclusion semantics",
        ):
            coverage_evidence(
                surface="executions",
                coverage_start="2026-09-24T19:00:00Z",
                coverage_end="2026-09-24T21:00:00Z",
                pagination_complete=True,
                consistency_horizon_satisfied=True,
                qualified_exclusion_semantics=True,
            
                account_id="paper-1",
                environment="PAPER",)



if __name__ == "__main__":
    unittest.main()
