from dataclasses import replace
import json
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.bybit_v5 import (
    parse_submission_response,
    prepare_order_submission,
)
from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
    stable_client_order_id,
)
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    observe_submission_json_response,
    provider_submission_observation_projection,
)
from mvp.autotrade_mvp.provider_route_dispatch import (
    compose_selected_provider_route_authority,
)
from mvp.autotrade_mvp.provider_route_financial_binding import (
    build_selected_provider_route_financial_submission_scope,
)
from mvp.autotrade_mvp.provider_selection import select_provider
from mvp.tests.test_bybit_v5 import write_capability
from mvp.tests.test_provider_route_dispatch import ProviderRouteDispatchTests
from mvp.tests.test_provider_selection import (
    NOW,
    candidate,
    request as route_request,
)


class ProviderRouteResponseCompositionTests(unittest.TestCase):
    def _selected_route(self, directory: str):
        fixture = ProviderRouteDispatchTests(
            methodName="test_success_binds_q_and_c_into_durable_submission_scope"
        )
        self.addCleanup(fixture.doCleanups)
        (
            journal,
            capabilities,
            qualifications,
            _old_route,
            dispatcher,
            _q1,
            _harness,
        ) = fixture.setup_route(directory)

        capability = write_capability(
            family="SPOT",
            position_mode="ONE_WAY",
            account_id="paper-account",
            environment="PAPER",
            instrument_version="BTCUSDT@1",
            expires_at=NOW.replace(hour=6),
            permission_scope="ORDER.WRITE",
            additional_permission_scopes=("ORDER_WRITE",),
            provider_environment="TESTNET",
        )
        self.assertTrue(capabilities.add(capability))
        route_candidate = replace(candidate(), entity_id=capability.entity_id)
        selection = select_provider(
            route_request(
                instrument_version=capability.instrument_version,
                time_in_force="GTC",
            ),
            [route_candidate],
            at=NOW,
            capability_registry=capabilities,
            qualification_registry=qualifications,
        )
        self.assertEqual(selection.status, "SELECTED_UNAMBIGUOUS")
        self.assertIsNotNone(selection.selected)
        return (
            journal,
            capabilities,
            qualifications,
            selection.selected,
            dispatcher,
            capability,
        )

    @staticmethod
    def _prepared(route, capability, *, intent_id: str):
        client_order_id = stable_client_order_id(
            "BYBIT",
            intent_id,
            environment="PAPER",
            account_id="paper-account",
            max_length=36,
            client_id_format="TOKEN",
        )
        prepared = prepare_order_submission(
            capability=capability,
            at=NOW,
            provider_environment=route.candidate.provider_environment,
            product_family="SPOT",
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity="0.001",
            client_order_id=client_order_id,
            time_in_force="GTC",
            price="50000",
        )
        scope = build_selected_provider_route_financial_submission_scope(
            route,
            account_id="paper-account",
            runtime_environment="PAPER",
            endpoint=prepared.endpoint,
            prepared_request_sha256=prepared.body_sha256,
            capability_snapshot_ids=prepared.capability_snapshot_ids,
            instrument_versions=prepared.instrument_versions,
        )
        return prepared, scope, client_order_id

    @staticmethod
    def _route_guard(journal, capabilities, qualifications, route):
        return compose_selected_provider_route_authority(
            store=journal,
            environment="PAPER",
            account_id="paper-account",
            route=route,
            capability_registry=capabilities,
            qualification_registry=qualifications,
            authority_check=lambda _intent_hash, _at: (
                True,
                "financial_authority_current",
            ),
        )

    def test_selected_route_financial_scope_survives_durable_response_normalization(self):
        with TemporaryDirectory() as directory:
            (
                journal,
                capabilities,
                qualifications,
                route,
                dispatcher,
                capability,
            ) = self._selected_route(directory)
            intent_id = "route-response-ack"
            attempt_id = "11111111-1111-4111-8111-111111111111"
            prepared, scope, client_order_id = self._prepared(
                route,
                capability,
                intent_id=intent_id,
            )
            raw = json.dumps(
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "provider-route-1",
                        "orderLinkId": client_order_id,
                    },
                    "retExtInfo": {},
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            route_guard = self._route_guard(
                journal,
                capabilities,
                qualifications,
                route,
            )

            outcome = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="sha256:" + "2" * 64,
                provider="BYBIT",
                request=dict(prepared.body),
                now=NOW.isoformat().replace("+00:00", "Z"),
                authority_check=route_guard,
                transport_send=lambda _cid, _request, final_guard: (
                    final_guard(),
                    ExactJsonTransportResponse(raw, http_status=200),
                )[1],
                sender_check=lambda _owner, _epoch: None,
                submission_scope=scope,
            )
            self.assertEqual(outcome.status, "SENT")

            binding = load_submission_response_binding(
                journal,
                environment="PAPER",
                account_id="paper-account",
                attempt_id=attempt_id,
            )
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="BYBIT",
                endpoint=prepared.endpoint,
                prepared_request_sha256=prepared.body_sha256,
                capability_snapshot_ids=prepared.capability_snapshot_ids,
                instrument_versions=prepared.instrument_versions,
            )
            projected = provider_submission_observation_projection(observation)
            self.assertEqual(
                projected["submission_scope"]["provider_route_qualification_id"],
                route.qualification_id,
            )
            self.assertEqual(
                projected["submission_scope"][
                    "provider_route_capability_snapshot_id"
                ],
                route.capability_snapshot_id,
            )
            self.assertEqual(
                projected["submission_scope"]["provider_route_adapter_code_sha"],
                route.candidate.adapter_code_sha,
            )
            self.assertEqual(
                projected["submission_scope"][
                    "provider_route_packaged_artifact_digest"
                ],
                route.candidate.packaged_artifact_digest,
            )

            normalized = parse_submission_response(
                attempt_id=attempt_id,
                prepared_request=prepared,
                observation=observation,
            )
            self.assertEqual(normalized["outcome"], "ACKNOWLEDGED")
            self.assertEqual(normalized["retry_disposition"], "NEVER")
            self.assertEqual(normalized["provider_order_id"], "provider-route-1")
            self.assertNotIn("fill", repr(normalized).lower())

    def test_route_scoped_ambiguous_response_stays_reconcile_first(self):
        with TemporaryDirectory() as directory:
            (
                journal,
                capabilities,
                qualifications,
                route,
                dispatcher,
                capability,
            ) = self._selected_route(directory)
            intent_id = "route-response-unknown"
            attempt_id = "22222222-2222-4222-8222-222222222222"
            prepared, scope, client_order_id = self._prepared(
                route,
                capability,
                intent_id=intent_id,
            )
            raw = json.dumps(
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "orderId": "provider-route-ambiguous",
                        "orderLinkId": client_order_id,
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            route_guard = self._route_guard(
                journal,
                capabilities,
                qualifications,
                route,
            )

            outcome = dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash="sha256:" + "3" * 64,
                provider="BYBIT",
                request=dict(prepared.body),
                now=NOW.isoformat().replace("+00:00", "Z"),
                authority_check=route_guard,
                transport_send=lambda _cid, _request, final_guard: (
                    final_guard(),
                    ExactJsonTransportResponse(
                        raw,
                        http_status=200,
                        requires_reconciliation=True,
                        ambiguity_reason="provider response requires reconciliation",
                    ),
                )[1],
                sender_check=lambda _owner, _epoch: None,
                submission_scope=scope,
            )
            self.assertEqual(outcome.status, "UNKNOWN")

            binding = load_submission_response_binding(
                journal,
                environment="PAPER",
                account_id="paper-account",
                attempt_id=attempt_id,
            )
            self.assertEqual(binding.terminal_state, "UNKNOWN")
            self.assertEqual(binding.retry_disposition, "RECONCILE_FIRST")
            with self.assertRaisesRegex(
                ProviderCoreError,
                "requires definitive SENT response",
            ):
                observe_submission_json_response(
                    response_binding=binding,
                    provider_id="BYBIT",
                    endpoint=prepared.endpoint,
                    prepared_request_sha256=prepared.body_sha256,
                    capability_snapshot_ids=prepared.capability_snapshot_ids,
                    instrument_versions=prepared.instrument_versions,
                )

            normalized = parse_submission_response(
                attempt_id=attempt_id,
                prepared_request=prepared,
                transport_ambiguous=True,
            )
            self.assertEqual(normalized["outcome"], "UNKNOWN")
            self.assertEqual(normalized["retry_disposition"], "RECONCILE_FIRST")
            self.assertEqual(normalized["evidence"], [])


if __name__ == "__main__":
    unittest.main()
