from datetime import timedelta
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import provider_core as provider_core_module
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.provider_core import AuthenticatedReadQueryBinding
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_origin import (
    ProviderOriginError,
    record_product_authenticated_read_origin,
)
from mvp.autotrade_mvp.provider_transport import (
    ProviderTransportError,
    ProviderTransportScopeError,
    resolve_authenticated_read_route_authority,
)
from mvp.tests.test_provider_origin import _provider_journal, selected_authority
from mvp.autotrade_mvp.provider_qualification_authority import (
    ProviderQualificationCurrentReader,
)
from mvp.tests.test_provider_transport import (
    READ_NOW,
    authenticated_read_binding,
    verified_read_capability,
)


class ProductProviderOriginCompositionTests(unittest.TestCase):
    def setUp(self):
        self.qualification_reader = object.__new__(
            ProviderQualificationCurrentReader
        )
        self.capability_registry = CapabilityRegistry()
        self.current_qualification = object()
        self.current_capability = verified_read_capability()
        self.current_authority_patch = patch(
            "mvp.autotrade_mvp.provider_origin.revalidate_selected_provider_authority",
            return_value=(
                self.current_qualification,
                self.current_capability,
            ),
        )
        self.current_authority = self.current_authority_patch.start()
        self.addCleanup(self.current_authority_patch.stop)

        self.prepared_authority_patch = patch(
            "mvp.autotrade_mvp.provider_transport.product_authenticated_read_prepared_authority",
            side_effect=lambda _transport, query: self._prepared_authority(query),
        )
        self.prepared_authority = self.prepared_authority_patch.start()
        self.addCleanup(self.prepared_authority_patch.stop)

    @staticmethod
    def _prepared_authority(query):
        route = resolve_authenticated_read_route_authority(query)
        return {
            "schema_version": "product-authenticated-read-prepared-authority:v1",
            "provider_id": query.provider_id,
            "account_id": query.account_id,
            "entity_id": query.entity_id,
            "environment": query.environment,
            "provider_environment": query.provider_environment,
            "capability_snapshot_id": query.capability_snapshot_id,
            "instrument_version": query.instrument_version,
            "surface": query.surface.value,
            "endpoint": query.endpoint,
            "permission_scope": query.permission_scope,
            "data_entitlement": route.data_entitlement,
            "success_statuses": tuple(route.success_statuses),
            "route_identity": route.route_identity,
            "transport_identity": "UrllibJsonWireClient:v1",
            "network_policy_identity": route.network_policy_identity,
            "credential_handle_identity": "sha256:" + "4" * 64,
            "credential_generation": 1,
            "validated_at": (READ_NOW + timedelta(seconds=1))
            .isoformat()
            .replace("+00:00", "Z"),
        }

    def test_imported_preparation_token_cannot_relabel_current_capability_scope(self):
        genuine = authenticated_read_binding()
        selected = selected_authority(genuine)
        material = {
            "provider_id": genuine.provider_id,
            "account_id": "caller-forged-account",
            "entity_id": genuine.entity_id,
            "environment": genuine.environment,
            "provider_environment": genuine.provider_environment,
            "capability_snapshot_id": genuine.capability_snapshot_id,
            "instrument_version": genuine.instrument_version,
            "surface": genuine.surface.value,
            "endpoint": genuine.endpoint,
            "query": dict(genuine.query),
            "prepared_at": genuine.prepared_at,
            "permission_scope": genuine.permission_scope,
        }
        forged = AuthenticatedReadQueryBinding(
            provider_id=material["provider_id"],
            account_id=material["account_id"],
            entity_id=material["entity_id"],
            environment=material["environment"],
            provider_environment=material["provider_environment"],
            capability_snapshot_id=material["capability_snapshot_id"],
            instrument_version=material["instrument_version"],
            surface=genuine.surface,
            endpoint=material["endpoint"],
            query=material["query"],
            prepared_at=material["prepared_at"],
            permission_scope=material["permission_scope"],
            query_digest="sha256:" + sha256(
                json.dumps(
                    material,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
            _preparation_token=provider_core_module._PREPARED_READ_TOKEN,
        )

        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            with patch(
                "mvp.autotrade_mvp.provider_transport.product_authenticated_read_transport_identity",
                return_value="UrllibJsonWireClient:v1",
            ), patch(
                "mvp.autotrade_mvp.provider_transport.execute_product_authenticated_read",
                side_effect=AssertionError("wire must not run for caller-relabeled scope"),
            ) as execute:
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "does not match current capability authority",
                ):
                    record_product_authenticated_read_origin(
                        origin_journal=journal,
                        product_transport=object(),
                        query_binding=forged,
                        selected_authority=selected,
                        qualification_reader=self.qualification_reader,
                        capability_registry=self.capability_registry,
                    )

            execute.assert_not_called()
            self.assertEqual(
                journal._store.load_events_by_aggregate_type(
                    "authenticated_provider_read"
                ),
                [],
            )
            self.assertEqual(self.current_authority.call_count, 1)

    def test_qc_change_after_prepare_blocks_wire_and_leaves_prepared_only(self):
        query = authenticated_read_binding()
        selected = selected_authority(query)
        replacement_q = object()
        self.current_authority.side_effect = [
            (self.current_qualification, self.current_capability),
            (replacement_q, self.current_capability),
        ]

        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            with patch(
                "mvp.autotrade_mvp.provider_transport.product_authenticated_read_transport_identity",
                return_value="UrllibJsonWireClient:v1",
            ), patch(
                "mvp.autotrade_mvp.provider_transport.execute_product_authenticated_read",
                side_effect=AssertionError("wire must not run after Q+C change"),
            ) as execute:
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "changed during authenticated-read prepare",
                ):
                    record_product_authenticated_read_origin(
                        origin_journal=journal,
                        product_transport=object(),
                        query_binding=query,
                        selected_authority=selected,
                        qualification_reader=self.qualification_reader,
                        capability_registry=self.capability_registry,
                    )

            execute.assert_not_called()
            events = journal._store.load_events_by_aggregate_type(
                "authenticated_provider_read"
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "AuthenticatedReadPrepared")
            self.assertEqual(self.current_authority.call_count, 2)


    def test_prepare_is_durable_before_product_wire_and_receipt_publishes_observed(self):
        query = authenticated_read_binding()
        selected = selected_authority(query)
        route = resolve_authenticated_read_route_authority(query)
        body = b'{"balances":[]}'
        receipt = object()

        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            wire_seen = []

            def execute(product_transport, exact_query):
                self.assertIs(product_transport, transport)
                self.assertIs(exact_query, query)
                events = JournalStore.load_events(
                    journal._store,
                    "authenticated_provider_read",
                    next(
                        event["aggregate_id"]
                        for event in journal._store.load_events_by_aggregate_type(
                            "authenticated_provider_read"
                        )
                    ),
                )
                self.assertEqual(
                    [event["event_type"] for event in events],
                    ["AuthenticatedReadPrepared"],
                )
                wire_seen.append(exact_query.query_digest)
                return receipt

            def validate(exact_receipt, exact_query):
                self.assertIs(exact_receipt, receipt)
                self.assertIs(exact_query, query)
                return (
                    "UrllibJsonWireClient:v1",
                    route.network_policy_identity,
                    200,
                    body,
                    READ_NOW + timedelta(seconds=1),
                )

            transport = object()
            with patch(
                "mvp.autotrade_mvp.provider_transport.product_authenticated_read_transport_identity",
                return_value="UrllibJsonWireClient:v1",
            ), patch(
                "mvp.autotrade_mvp.provider_transport.execute_product_authenticated_read",
                side_effect=execute,
            ), patch(
                "mvp.autotrade_mvp.provider_transport.validate_product_authenticated_read_receipt",
                side_effect=validate,
            ), patch(
                "mvp.autotrade_mvp.provider_transport.retire_product_authenticated_read_receipt",
            ) as retire:
                binding = record_product_authenticated_read_origin(
                    origin_journal=journal,
                    product_transport=transport,
                    query_binding=query,
                    selected_authority=selected,
                    qualification_reader=self.qualification_reader,
                    capability_registry=self.capability_registry,
                )

            self.assertEqual(wire_seen, [query.query_digest])
            self.assertEqual(binding.response_bytes, body)
            self.assertEqual(binding.http_status, 200)
            events = JournalStore.load_events(
                journal._store,
                "authenticated_provider_read",
                binding.attempt_id,
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "AuthenticatedReadPrepared",
                    "AuthenticatedReadResponseRetained",
                    "AuthenticatedReadObserved",
                ],
            )
            retire.assert_called_once_with(receipt)

    def test_retained_cut_retires_receipt_before_observed_commit(self):
        query = authenticated_read_binding()
        selected = selected_authority(query)
        route = resolve_authenticated_read_route_authority(query)
        body = b'{"balances":[]}'
        receipt = object()

        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            original_append = JournalStore.append_protected_event

            def fail_observed(
                store,
                capability,
                envelope,
                *,
                outbox_topic=None,
            ):
                if envelope.get("event_type") == "AuthenticatedReadObserved":
                    raise RuntimeError("forced observed commit failure")
                return original_append(
                    store,
                    capability,
                    envelope,
                    outbox_topic=outbox_topic,
                )

            with patch(
                "mvp.autotrade_mvp.provider_transport.product_authenticated_read_transport_identity",
                return_value="UrllibJsonWireClient:v1",
            ), patch(
                "mvp.autotrade_mvp.provider_transport.execute_product_authenticated_read",
                return_value=receipt,
            ), patch(
                "mvp.autotrade_mvp.provider_transport.validate_product_authenticated_read_receipt",
                return_value=(
                    "UrllibJsonWireClient:v1",
                    route.network_policy_identity,
                    200,
                    body,
                    READ_NOW + timedelta(seconds=1),
                ),
            ), patch(
                "mvp.autotrade_mvp.provider_transport.retire_product_authenticated_read_receipt",
                return_value=None,
            ) as retire, patch.object(
                JournalStore,
                "append_protected_event",
                new=fail_observed,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "forced observed commit failure",
                ):
                    record_product_authenticated_read_origin(
                        origin_journal=journal,
                        product_transport=object(),
                        query_binding=query,
                        selected_authority=selected,
                        qualification_reader=self.qualification_reader,
                        capability_registry=self.capability_registry,
                    )

            retire.assert_called_once_with(receipt)
            events = journal._store.load_events_by_aggregate_type(
                "authenticated_provider_read"
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "AuthenticatedReadPrepared",
                    "AuthenticatedReadResponseRetained",
                ],
            )
            recovered = journal.recover_response_binding(
                events[0]["aggregate_id"],
                query,
            )
            self.assertEqual(recovered.response_bytes, body)
            self.assertEqual(recovered.http_status, 200)

    def test_retirement_failure_stops_before_observed_and_leaves_recovery_state(self):
        query = authenticated_read_binding()
        selected = selected_authority(query)
        route = resolve_authenticated_read_route_authority(query)
        body = b'{"balances":[]}'
        receipt = object()

        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            with patch(
                "mvp.autotrade_mvp.provider_transport.product_authenticated_read_transport_identity",
                return_value="UrllibJsonWireClient:v1",
            ), patch(
                "mvp.autotrade_mvp.provider_transport.execute_product_authenticated_read",
                return_value=receipt,
            ), patch(
                "mvp.autotrade_mvp.provider_transport.validate_product_authenticated_read_receipt",
                return_value=(
                    "UrllibJsonWireClient:v1",
                    route.network_policy_identity,
                    200,
                    body,
                    READ_NOW + timedelta(seconds=1),
                ),
            ), patch(
                "mvp.autotrade_mvp.provider_transport.retire_product_authenticated_read_receipt",
                side_effect=ProviderTransportScopeError("receipt registry changed"),
            ):
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "could not retire wire receipt",
                ):
                    record_product_authenticated_read_origin(
                        origin_journal=journal,
                        product_transport=object(),
                        query_binding=query,
                        selected_authority=selected,
                        qualification_reader=self.qualification_reader,
                        capability_registry=self.capability_registry,
                    )

            events = journal._store.load_events_by_aggregate_type(
                "authenticated_provider_read"
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "AuthenticatedReadPrepared",
                    "AuthenticatedReadResponseRetained",
                ],
            )
            recovered = journal.recover_response_binding(
                events[0]["aggregate_id"],
                query,
            )
            self.assertEqual(recovered.response_bytes, body)


    def test_recovery_is_idempotent_after_terminal_observed(self):
        query = authenticated_read_binding()
        selected = selected_authority(query)
        route = resolve_authenticated_read_route_authority(query)
        body = b'{"balances":[]}'
        receipt = object()

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = _provider_journal(JournalStore(path))
            original_append = JournalStore.append_protected_event

            def fail_observed_once(
                store,
                capability,
                envelope,
                *,
                outbox_topic=None,
            ):
                if envelope.get("event_type") == "AuthenticatedReadObserved":
                    raise RuntimeError("forced observed commit failure")
                return original_append(
                    store,
                    capability,
                    envelope,
                    outbox_topic=outbox_topic,
                )

            with patch(
                "mvp.autotrade_mvp.provider_transport.product_authenticated_read_transport_identity",
                return_value="UrllibJsonWireClient:v1",
            ), patch(
                "mvp.autotrade_mvp.provider_transport.execute_product_authenticated_read",
                return_value=receipt,
            ), patch(
                "mvp.autotrade_mvp.provider_transport.validate_product_authenticated_read_receipt",
                return_value=(
                    "UrllibJsonWireClient:v1",
                    route.network_policy_identity,
                    200,
                    body,
                    READ_NOW + timedelta(seconds=1),
                ),
            ), patch(
                "mvp.autotrade_mvp.provider_transport.retire_product_authenticated_read_receipt",
                return_value=None,
            ), patch.object(
                JournalStore,
                "append_protected_event",
                new=fail_observed_once,
            ):
                with self.assertRaisesRegex(RuntimeError, "forced observed"):
                    record_product_authenticated_read_origin(
                        origin_journal=journal,
                        product_transport=object(),
                        query_binding=query,
                        selected_authority=selected,
                        qualification_reader=self.qualification_reader,
                        capability_registry=self.capability_registry,
                    )

            events = journal._store.load_events_by_aggregate_type(
                "authenticated_provider_read"
            )
            attempt_id = events[0]["aggregate_id"]
            restarted = _provider_journal(JournalStore(path))
            first = restarted.recover_response_binding(attempt_id, query)
            first_events = restarted._store.load_events_by_aggregate_type(
                "authenticated_provider_read"
            )
            second = restarted.recover_response_binding(attempt_id, query)
            second_events = restarted._store.load_events_by_aggregate_type(
                "authenticated_provider_read"
            )

            self.assertEqual(second, first)
            self.assertEqual(len(first_events), 3)
            self.assertEqual(second_events, first_events)


    def test_wire_failure_leaves_prepared_only_and_cannot_escape_origin(self):
        query = authenticated_read_binding()
        selected = selected_authority(query)

        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            with patch(
                "mvp.autotrade_mvp.provider_transport.product_authenticated_read_transport_identity",
                return_value="UrllibJsonWireClient:v1",
            ), patch(
                "mvp.autotrade_mvp.provider_transport.execute_product_authenticated_read",
                side_effect=ProviderTransportError("no definitive response"),
            ):
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "did not produce durable origin evidence",
                ):
                    record_product_authenticated_read_origin(
                        origin_journal=journal,
                        product_transport=object(),
                        query_binding=query,
                        selected_authority=selected,
                        qualification_reader=self.qualification_reader,
                        capability_registry=self.capability_registry,
                    )

            events = journal._store.load_events_by_aggregate_type(
                "authenticated_provider_read"
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "AuthenticatedReadPrepared")


    def test_prepared_authority_failure_creates_no_journal_state_or_wire_io(self):
        query = authenticated_read_binding()
        selected = selected_authority(query)

        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            with patch(
                "mvp.autotrade_mvp.provider_transport.product_authenticated_read_prepared_authority",
                side_effect=ProviderTransportScopeError(
                    "caller binding has no current capability authority"
                ),
            ), patch(
                "mvp.autotrade_mvp.provider_transport.execute_product_authenticated_read",
                side_effect=AssertionError("wire must not execute"),
            ) as execute:
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "prepared authority is unavailable",
                ):
                    record_product_authenticated_read_origin(
                        origin_journal=journal,
                        product_transport=object(),
                        query_binding=query,
                        selected_authority=selected,
                        qualification_reader=self.qualification_reader,
                        capability_registry=self.capability_registry,
                    )

            execute.assert_not_called()
            self.assertEqual(
                journal._store.load_events_by_aggregate_type(
                    "authenticated_provider_read"
                ),
                [],
            )

    def test_prepared_transport_authority_change_after_prepare_blocks_wire(self):
        query = authenticated_read_binding()
        selected = selected_authority(query)
        first = self._prepared_authority(query)
        second = dict(first)
        second["credential_generation"] = first["credential_generation"] + 1
        self.prepared_authority.side_effect = [first, second]

        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            with patch(
                "mvp.autotrade_mvp.provider_transport.execute_product_authenticated_read",
                side_effect=AssertionError("wire must not execute after authority change"),
            ) as execute:
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "prepared authority changed during durable prepare",
                ):
                    record_product_authenticated_read_origin(
                        origin_journal=journal,
                        product_transport=object(),
                        query_binding=query,
                        selected_authority=selected,
                        qualification_reader=self.qualification_reader,
                        capability_registry=self.capability_registry,
                    )

            execute.assert_not_called()
            events = journal._store.load_events_by_aggregate_type(
                "authenticated_provider_read"
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "AuthenticatedReadPrepared")


if __name__ == "__main__":
    unittest.main()
