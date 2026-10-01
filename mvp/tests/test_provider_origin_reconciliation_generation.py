from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_origin import (
    ProviderOriginError,
    observe_provider_origin_json_response,
    record_product_authenticated_read_origin,
)
from mvp.autotrade_mvp.provider_qualification_authority import (
    ProviderQualificationCurrentReader,
)
from mvp.autotrade_mvp.provider_transport import (
    resolve_authenticated_read_route_authority,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    prepare_reconciliation_scope_generation,
    reconciliation_scope_generation_payload,
)
from mvp.tests.test_provider_origin import _provider_journal, selected_authority
from mvp.tests.test_provider_transport import (
    authenticated_read_binding,
    verified_read_capability,
)


def _reconciliation_selected(query):
    return replace(
        selected_authority(query),
        qualification_id="sha256:" + "2" * 64,
        reconciliation_semantics_id="sha256:" + "3" * 64,
    )


def _prepare_generation(store, selected, request_number):
    return prepare_reconciliation_scope_generation(
        store,
        selected_authority=selected,
        acquisition_request_id=str(UUID(int=request_number)),
        host_id="host-provider-origin-generation-test",
        owner_epoch="epoch-1",
    )


def _unrelated_envelope():
    event_id = str(uuid4())
    instant = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {"kind": "provider-origin-generation-race"}
    return {
        "event_id": event_id,
        "event_type": "ProviderOriginGenerationRaceFixture",
        "schema_version": "1.0.0",
        "aggregate_type": "provider_origin_generation_race_fixture",
        "aggregate_id": "race-fixture",
        "aggregate_version": "1",
        "host_id": "test-host",
        "owner_epoch": "test-epoch",
        "environment": "SIMULATION",
        "occurred_at": instant,
        "observed_at": instant,
        "committed_at": instant,
        "correlation_id": event_id,
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }


class ProviderOriginReconciliationGenerationTests(unittest.TestCase):
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
        self.current_authority_patch.start()
        self.addCleanup(self.current_authority_patch.stop)

    @staticmethod
    def _prepared_authority(query):
        route = resolve_authenticated_read_route_authority(query)
        validated_at = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
        validated_at = validated_at.replace("+00:00", "Z")
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
            "validated_at": validated_at,
        }

    def _record(self, journal, query, selected, generation, *, prepared=None):
        route = resolve_authenticated_read_route_authority(query)
        body = b'{"balances":[]}'
        receipt = object()
        prepared = prepared or (
            lambda _transport, exact_query: self._prepared_authority(exact_query)
        )
        with patch(
            "mvp.autotrade_mvp.provider_transport.product_authenticated_read_prepared_authority",
            side_effect=prepared,
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
                datetime.now(timezone.utc) + timedelta(seconds=3),
            ),
        ), patch(
            "mvp.autotrade_mvp.provider_transport.retire_product_authenticated_read_receipt",
            return_value=None,
        ):
            return record_product_authenticated_read_origin(
                origin_journal=journal,
                product_transport=object(),
                query_binding=query,
                selected_authority=selected,
                qualification_reader=self.qualification_reader,
                capability_registry=self.capability_registry,
                reconciliation_generation=generation,
            )

    def test_generation_is_bound_through_three_event_retention_and_restart(self):
        query = authenticated_read_binding()
        selected = _reconciliation_selected(query)
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = _provider_journal(JournalStore(path))
            generation = _prepare_generation(journal._store, selected, 101)
            expected_generation = reconciliation_scope_generation_payload(generation)

            binding = self._record(journal, query, selected, generation)
            self.assertEqual(dict(binding.reconciliation_generation), expected_generation)

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
            for event in events:
                self.assertEqual(
                    event["payload"]["reconciliation_generation"],
                    expected_generation,
                )
            self.assertLess(
                generation.journal_sequence,
                events[0]["journal_sequence"],
            )

            manifest, retained_bytes = journal._response_reader(
                binding.response_artifact_id
            )
            self.assertEqual(
                manifest["metadata"]["reconciliation_generation"],
                expected_generation,
            )
            self.assertEqual(retained_bytes, binding.response_bytes)

            restarted = _provider_journal(JournalStore(path))
            recovered = restarted.load_response_binding(binding.attempt_id, query)
            self.assertEqual(recovered, binding)
            self.assertEqual(
                dict(recovered.reconciliation_generation),
                expected_generation,
            )

    def test_generation_superseded_after_durable_prepare_blocks_provider_wire(self):
        query = authenticated_read_binding()
        selected = _reconciliation_selected(query)
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            generation = _prepare_generation(journal._store, selected, 102)
            prepared_calls = 0

            def prepared(_transport, exact_query):
                nonlocal prepared_calls
                prepared_calls += 1
                if prepared_calls == 2:
                    _prepare_generation(journal._store, selected, 103)
                return self._prepared_authority(exact_query)

            with patch(
                "mvp.autotrade_mvp.provider_transport.product_authenticated_read_prepared_authority",
                side_effect=prepared,
            ), patch(
                "mvp.autotrade_mvp.provider_transport.execute_product_authenticated_read",
                side_effect=AssertionError("wire must not run for superseded generation"),
            ) as execute:
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "no longer authoritative",
                ):
                    record_product_authenticated_read_origin(
                        origin_journal=journal,
                        product_transport=object(),
                        query_binding=query,
                        selected_authority=selected,
                        qualification_reader=self.qualification_reader,
                        capability_registry=self.capability_registry,
                        reconciliation_generation=generation,
                    )

            execute.assert_not_called()
            events = journal._store.load_events_by_aggregate_type(
                "authenticated_provider_read"
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "AuthenticatedReadPrepared")

    def test_global_journal_cut_race_cannot_publish_provider_prepared(self):
        query = authenticated_read_binding()
        selected = _reconciliation_selected(query)
        route = resolve_authenticated_read_route_authority(query)
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            generation = _prepare_generation(journal._store, selected, 104)
            original_append = JournalStore.append_protected_event
            raced = False

            def raced_append(
                store,
                capability,
                envelope,
                *,
                outbox_topic=None,
                aggregate_preconditions=(),
                expected_journal_sequence=None,
            ):
                nonlocal raced
                if not raced:
                    raced = True
                    JournalStore.append_event(store, _unrelated_envelope())
                return original_append(
                    store,
                    capability,
                    envelope,
                    outbox_topic=outbox_topic,
                    aggregate_preconditions=aggregate_preconditions,
                    expected_journal_sequence=expected_journal_sequence,
                )

            with patch.object(
                JournalStore,
                "append_protected_event",
                new=raced_append,
            ):
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "journal changed after reconciliation-generation validation",
                ):
                    journal.prepare(
                        query,
                        selected_authority=selected,
                        transport_identity="UrllibJsonWireClient:v1",
                        network_policy_identity=route.network_policy_identity,
                        recorded_at=datetime.now(timezone.utc)
                        + timedelta(seconds=2),
                        reconciliation_generation=generation,
                    )

            self.assertTrue(raced)
            self.assertEqual(
                journal._store.load_events_by_aggregate_type(
                    "authenticated_provider_read"
                ),
                [],
            )

    def test_superseded_generation_reloads_historical_bytes_but_blocks_promotion(self):
        query = authenticated_read_binding()
        selected = _reconciliation_selected(query)
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = _provider_journal(JournalStore(path))
            generation = _prepare_generation(journal._store, selected, 105)
            binding = self._record(journal, query, selected, generation)

            _prepare_generation(journal._store, selected, 106)

            restarted = _provider_journal(JournalStore(path))
            historical = restarted.load_response_binding(binding.attempt_id, query)
            self.assertEqual(historical, binding)
            self.assertEqual(
                dict(historical.reconciliation_generation),
                reconciliation_scope_generation_payload(generation),
            )

            with self.assertRaisesRegex(
                ProviderOriginError,
                "no longer authoritative",
            ):
                observe_provider_origin_json_response(
                    journal=restarted,
                    response_binding=historical,
                    query_binding=query,
                    required_data_entitlement="ACCOUNT",
                    qualification_reader=self.qualification_reader,
                    capability_registry=self.capability_registry,
                )


if __name__ == "__main__":
    unittest.main()
