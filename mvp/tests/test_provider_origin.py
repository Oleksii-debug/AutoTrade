from copy import copy
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp import provider_origin as provider_origin_module
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.persistence import (
    JournalStore,
    canonical_json,
    payload_digest,
)
from mvp.autotrade_mvp.provider_core import (
    Surface,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.provider_origin import (
    AuthenticatedReadResponseBinding,
    HistoricalProviderOriginEvidence,
    ProviderOriginError,
    ProviderOriginJournal,
    ProviderOriginObservation,
    observe_provider_origin_json_response,
)
from mvp.autotrade_mvp.provider_qualification_authority import (
    ProviderQualificationCurrentReader,
)
from mvp.autotrade_mvp.provider_selection import (
    ProviderSelectionError,
    SelectedProviderAuthority,
)
from mvp.autotrade_mvp.provider_transport import (
    BINANCE_SPOT_AUTHENTICATED_READ_ENDPOINTS,
    canonical_authenticated_read_route,
)
from mvp.tests.test_bybit_v5 import READ_AT, read_capability
from mvp.tests.test_provider_transport import (
    READ_NOW,
    authenticated_read_binding,
)


def _provider_writer(store: JournalStore):
    return JournalStore.bind_protected_writer(
        store,
        aggregate_type=provider_origin_module._AGGREGATE_TYPE,
        namespace=provider_origin_module._PROTECTED_WRITER_NAMESPACE,
        writer_authority_id=provider_origin_module._PROTECTED_WRITER_AUTHORITY_ID,
    )


def _provider_journal(store: JournalStore) -> ProviderOriginJournal:
    response_store = ArtifactStore(
        Path(store.path).parent / "provider-origin-artifacts"
    )
    return ProviderOriginJournal(
        store,
        writer_capability=_provider_writer(store),
        response_store=response_store,
    )



def selected_authority(query) -> SelectedProviderAuthority:
    product_family = {
        "BINANCE": "SPOT",
        "BYBIT": "SPOT",
        "KRAKEN": "SPOT",
        "ALPACA": "EQUITIES",
    }[query.provider_id]
    return SelectedProviderAuthority(
        provider_id=query.provider_id,
        product_family=product_family,
        adapter_code_sha="1" * 40,
        qualification_id="qualification:test:v1",
        capability_snapshot_id=query.capability_snapshot_id,
        account_id=query.account_id,
        entity_id=query.entity_id,
        environment=query.environment,
        provider_environment=query.provider_environment,
        instrument_version=query.instrument_version,
        route_policy_id=query.provider_id.lower() + "-qualified-route-v1",
        entity_policy_id=query.provider_id.lower() + "-entity-v1",
        network_policy_id="direct-tls-v1",
        account_class="STANDARD",
        release_artifact_id=None,
        release_artifact_sha256=None,
        reconciliation_semantics_id=None,
    )



def _legacy_route_snapshot(query) -> dict[str, object]:
    current = provider_origin_module._current_route_snapshot(query)
    material = {
        key: value
        for key, value in current.items()
        if key not in {"network_policy_identity", "route_digest"}
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return {
        **material,
        "route_digest": "sha256:" + sha256(encoded).hexdigest(),
    }


def _append_pre_q_network_provider_origin(
    journal: ProviderOriginJournal,
    query,
    *,
    attempt_id: str,
    body: bytes,
) -> None:
    route = _legacy_route_snapshot(query)
    query_snapshot = provider_origin_module._query_snapshot(query)
    prepared_at = READ_NOW.isoformat().replace("+00:00", "Z")
    observed_at = (READ_NOW + timedelta(seconds=1)).isoformat().replace(
        "+00:00", "Z"
    )
    prepared_event_id = attempt_id + ":prepared"
    observed_event_id = attempt_id + ":observed"
    transport_identity = "UrllibJsonWireClient:v1"
    historical_network_policy = "sha256:" + "4" * 64
    prepared_payload = {
        "origin_kind": "PROVIDER_ORIGIN",
        "query": query_snapshot,
        "transport_identity": transport_identity,
        "network_policy_identity": historical_network_policy,
        "route": route,
    }
    JournalStore.append_protected_event(
        journal._store,
        journal._writer_capability,
        ProviderOriginJournal._event(
            event_id=prepared_event_id,
            event_type="AuthenticatedReadPrepared",
            attempt_id=attempt_id,
            aggregate_version=1,
            payload=prepared_payload,
            committed_at=prepared_at,
        ),
    )

    response_digest = "sha256:" + sha256(body).hexdigest()
    response_artifact_id = provider_origin_module._response_artifact_id(
        attempt_id=attempt_id,
        query_digest=query.query_digest,
        response_sha256=response_digest,
    )
    journal._response_store.publish_bytes(
        artifact_id=response_artifact_id,
        data=body,
        media_type="application/octet-stream",
        rights={
            "storage": True,
            "export": False,
            "rights_id": "provider-origin-response:v1",
        },
        source_refs=[],
        metadata={
            "evidence_kind": "PROVIDER_ORIGIN_RESPONSE",
            "attempt_id": attempt_id,
            "provider_id": query.provider_id,
            "provider_environment": query.provider_environment,
            "query_digest": query.query_digest,
            "route_digest": route["route_digest"],
            "data_entitlement": route["data_entitlement"],
        },
    )
    observed_payload = {
        "origin_kind": "PROVIDER_ORIGIN",
        "prepared_event_id": prepared_event_id,
        "query_digest": query.query_digest,
        "transport_identity": transport_identity,
        "network_policy_identity": historical_network_policy,
        "route_digest": route["route_digest"],
        "http_status": 200,
        "response_sha256": response_digest,
        "response_artifact_id": response_artifact_id,
        "observed_at": observed_at,
    }
    JournalStore.append_protected_event(
        journal._store,
        journal._writer_capability,
        ProviderOriginJournal._event(
            event_id=observed_event_id,
            event_type="AuthenticatedReadObserved",
            attempt_id=attempt_id,
            aggregate_version=2,
            payload=observed_payload,
            committed_at=observed_at,
        ),
    )


def _promote_provider_origin(**kwargs):
    """Exercise current financial promotion with exact-type Q/C holders."""

    reader = object.__new__(ProviderQualificationCurrentReader)
    registry = CapabilityRegistry()
    with patch.object(
        provider_origin_module,
        "revalidate_selected_provider_authority",
        return_value=(object(), object()),
    ):
        return observe_provider_origin_json_response(
            qualification_reader=reader,
            capability_registry=registry,
            **kwargs,
        )


def _record_test_provider_origin(
    journal: ProviderOriginJournal,
    attempt_id: str,
    query_binding: AuthenticatedReadQueryBinding,
    *,
    http_status: int,
    response_bytes: bytes,
    observed_at: datetime,
) -> AuthenticatedReadResponseBinding:
    """Test-only wire oracle: product bytes enter only through receipt validation."""

    events = journal._load_protected_history(journal._store, attempt_id)
    if len(events) != 1:
        # Let production chronology produce the canonical failure.
        transport_identity = "unavailable"
        network_policy_identity = "sha256:" + "0" * 64
    else:
        payload = events[0]["payload"]
        transport_identity = payload["transport_identity"]
        network_policy_identity = payload["network_policy_identity"]
    receipt = object()
    with patch(
        "mvp.autotrade_mvp.provider_transport.validate_product_authenticated_read_receipt",
        return_value=(
            transport_identity,
            network_policy_identity,
            http_status,
            response_bytes,
            observed_at,
        ),
    ), patch(
        "mvp.autotrade_mvp.provider_transport.retire_product_authenticated_read_receipt",
        return_value=None,
    ):
        return journal._record_provider_origin(
            attempt_id,
            query_binding,
            wire_receipt=receipt,
        )


class ProviderOriginJournalTests(unittest.TestCase):
    def test_pre_q_network_schema_is_historical_only_after_upgrade(self):
        query = authenticated_read_binding()
        body = b'{"balances":[{"asset":"USDT","free":"42.00"}]}'
        attempt_id = "provider-read:legacy-q-network-boundary"
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = _provider_journal(JournalStore(path))
            _append_pre_q_network_provider_origin(
                journal,
                query,
                attempt_id=attempt_id,
                body=body,
            )

            restarted = _provider_journal(JournalStore(path))
            with self.assertRaisesRegex(
                ProviderOriginError,
                "payload schema is not exact",
            ):
                restarted.load_response_binding(attempt_id, query)

            historical = restarted.load_historical_response_evidence(
                attempt_id,
                query,
            )
            self.assertIs(type(historical), HistoricalProviderOriginEvidence)
            self.assertEqual(
                historical.evidence_schema,
                "provider-origin-historical:pre-q-network-v1",
            )
            self.assertEqual(historical.response_bytes, body)
            self.assertEqual(historical.http_status, 200)
            self.assertEqual(historical.provider_id, query.provider_id)
            self.assertEqual(
                historical.provider_environment,
                query.provider_environment,
            )
            self.assertTrue(
                historical.legacy_origin_ref.startswith("provider-origin:sha256:")
            )
            self.assertFalse(hasattr(historical, "origin_ref"))
            self.assertFalse(hasattr(historical, "qualification_id"))
            self.assertFalse(hasattr(historical, "adapter_code_sha"))
            self.assertFalse(hasattr(historical, "selection_identity"))

            with self.assertRaisesRegex(
                ProviderOriginError,
                "response_binding must be exact",
            ):
                _promote_provider_origin(
                    journal=restarted,
                    response_binding=historical,
                    query_binding=query,
                    required_data_entitlement="ACCOUNT",
                )

    def test_current_q_network_schema_cannot_be_downgraded_to_historical(self):
        query = authenticated_read_binding()
        body = b'{"balances":[{"asset":"USDT","free":"100.00"}]}'
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                selected_authority=selected_authority(query),
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "5" * 64,
                recorded_at=READ_NOW,
            )
            _record_test_provider_origin(
                journal,
                attempt_id,
                query,
                http_status=200,
                response_bytes=body,
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            current = journal.load_response_binding(attempt_id, query)
            self.assertTrue(current.origin_ref.startswith("provider-origin:sha256:"))
            with self.assertRaisesRegex(
                ProviderOriginError,
                "payload schema is not exact",
            ):
                journal.load_historical_response_evidence(attempt_id, query)

    def test_historical_route_schema_rejects_current_network_policy_field(self):
        query = authenticated_read_binding()
        legacy = _legacy_route_snapshot(query)
        current = provider_origin_module._current_route_snapshot(query)
        self.assertNotIn("network_policy_identity", legacy)
        self.assertIn("network_policy_identity", current)
        with self.assertRaisesRegex(
            ProviderOriginError,
            "historical authenticated-read route snapshot schema is not exact",
        ):
            provider_origin_module._validate_legacy_route_snapshot(
                dict(current),
                query,
            )

    def test_provider_origin_survives_restart_without_network_requery(self):
        query = authenticated_read_binding()
        body = b'{"balances":[{"asset":"USDT","free":"100.00"}]}'
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = _provider_journal(JournalStore(path))
            attempt_id = journal.prepare(
                query,
                selected_authority=selected_authority(query),
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "1" * 64,
                recorded_at=READ_NOW,
            )
            binding = _record_test_provider_origin(
                journal,
                attempt_id,
                query,
                http_status=200,
                response_bytes=body,
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            self.assertEqual(binding.response_bytes, body)
            self.assertEqual(binding.http_status, 200)
            events = journal._load_protected_history(
                journal._store,
                attempt_id,
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "AuthenticatedReadPrepared",
                    "AuthenticatedReadResponseRetained",
                    "AuthenticatedReadObserved",
                ],
            )
            retained_payload = events[1]["payload"]
            observed_payload = events[2]["payload"]
            self.assertNotIn("response_base64", retained_payload)
            self.assertEqual(
                retained_payload["response_artifact_id"],
                binding.response_artifact_id,
            )
            self.assertEqual(
                observed_payload["retained_event_id"],
                events[1]["event_id"],
            )
            manifest, retained = journal._response_reader(
                binding.response_artifact_id
            )
            self.assertEqual(retained, body)
            self.assertEqual(manifest["sha256"], binding.response_sha256)
            self.assertTrue(binding.origin_ref.startswith("provider-origin:sha256:"))
            self.assertGreater(binding.journal_sequence, 0)
            self.assertEqual(binding.qualification_id, "qualification:test:v1")
            self.assertEqual(binding.adapter_code_sha, "1" * 40)
            self.assertTrue(binding.selection_identity.startswith("sha256:"))
            self.assertEqual(
                events[0]["payload"]["selection"]["selection_identity"],
                binding.selection_identity,
            )
            self.assertEqual(
                events[2]["payload"]["selection_identity"],
                binding.selection_identity,
            )
            self.assertEqual(
                manifest["metadata"]["qualification_id"],
                binding.qualification_id,
            )

            restarted = _provider_journal(JournalStore(path))
            recovered = restarted.load_response_binding(attempt_id, query)
            self.assertEqual(recovered, binding)
            self.assertEqual(recovered.response_bytes, body)
            observation = _promote_provider_origin(
                journal=restarted,
                response_binding=recovered,
                query_binding=query,
                required_data_entitlement="ACCOUNT",
            )
            self.assertEqual(observation.origin_ref, binding.origin_ref)
            self.assertEqual(observation.provider_id, "BINANCE")
            self.assertEqual(observation.account_id, "acct-1")
            self.assertEqual(observation.environment, "PAPER")
            self.assertEqual(binding.provider_environment, "PAPER")
            self.assertEqual(recovered.provider_environment, "PAPER")
            self.assertEqual(observation.provider_environment, "PAPER")
            self.assertEqual(observation.payload["balances"][0]["free"], "100.00")

    def test_provider_origin_recovery_rejects_cross_provider_domain_binding(self):
        kwargs = dict(
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/v5/execution/list",
            query={"category": "spot", "limit": "100"},
            at=READ_AT,
            permission_scope="ORDER.READ",
        )
        testnet_capability = read_capability(provider_environment="TESTNET")
        demo_capability = read_capability(provider_environment="DEMO")
        testnet_query = prepare_authenticated_read_query(
            capability=testnet_capability,
            provider_environment="TESTNET",
            **kwargs,
        )
        demo_query = prepare_authenticated_read_query(
            capability=demo_capability,
            provider_environment="DEMO",
            **kwargs,
        )
        body = b'{"result":{"list":[]}}'

        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                testnet_query,
                selected_authority=selected_authority(testnet_query),
                transport_identity="BybitProductWire:v1",
                network_policy_identity="sha256:" + "9" * 64,
                recorded_at=READ_AT,
            )
            binding = _record_test_provider_origin(
                journal,
                attempt_id,
                testnet_query,
                http_status=200,
                response_bytes=body,
                observed_at=READ_AT + timedelta(seconds=1),
            )
            self.assertEqual(binding.provider_environment, "TESTNET")

            with self.assertRaisesRegex(
                ProviderOriginError,
                "query does not match exact query binding|query digest conflicts",
            ):
                journal.load_response_binding(attempt_id, demo_query)

            observation = _promote_provider_origin(
                journal=journal,
                response_binding=binding,
                query_binding=testnet_query,
                required_data_entitlement="EXECUTIONS",
            )
            self.assertEqual(observation.provider_environment, "TESTNET")

    def test_financial_promotion_revalidates_exact_durable_response_subject(self):
        query = authenticated_read_binding()
        body = b'{"balances":[{"asset":"USDT","free":"100.00"}]}'
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                selected_authority=selected_authority(query),
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "a" * 64,
                recorded_at=READ_NOW,
            )
            durable = _record_test_provider_origin(
                journal,
                attempt_id,
                query,
                http_status=200,
                response_bytes=body,
                observed_at=READ_NOW + timedelta(seconds=1),
            )

            forged = copy(durable)
            forged_body = b'{"balances":[{"asset":"USDT","free":"999999.00"}]}'
            object.__setattr__(forged, "response_bytes", forged_body)
            object.__setattr__(
                forged,
                "response_sha256",
                "sha256:" + sha256(forged_body).hexdigest(),
            )

            with self.assertRaisesRegex(
                ProviderOriginError,
                "changed after durable revalidation",
            ):
                _promote_provider_origin(
                    journal=journal,
                    response_binding=forged,
                    query_binding=query,
                required_data_entitlement="ACCOUNT",
                )

            observation = _promote_provider_origin(
                journal=journal,
                response_binding=durable,
                query_binding=query,
                required_data_entitlement="ACCOUNT",
            )
            self.assertEqual(
                observation.payload["balances"][0]["free"],
                "100.00",
            )

    def test_financial_promotion_revalidates_current_qc_after_durable_reload(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                selected_authority=selected_authority(query),
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "a" * 64,
                recorded_at=READ_NOW,
            )
            binding = _record_test_provider_origin(
                journal,
                attempt_id,
                query,
                http_status=200,
                response_bytes=b'{"balances":[]}',
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            reader = object.__new__(ProviderQualificationCurrentReader)
            registry = CapabilityRegistry()
            with patch.object(
                provider_origin_module,
                "revalidate_selected_provider_authority",
                side_effect=ProviderSelectionError("Q2/C2 replaced Q1/C1"),
            ) as revalidate:
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    r"Q\+C authority is no longer current",
                ):
                    observe_provider_origin_json_response(
                        journal=journal,
                        response_binding=binding,
                        query_binding=query,
                        required_data_entitlement="ACCOUNT",
                        qualification_reader=reader,
                        capability_registry=registry,
                    )
            revalidate.assert_called_once()
            selected = revalidate.call_args.args[0]
            self.assertIs(type(selected), SelectedProviderAuthority)
            self.assertEqual(selected.qualification_id, binding.qualification_id)
            self.assertEqual(
                selected.capability_snapshot_id,
                binding.capability_snapshot_id,
            )
            self.assertEqual(selected.provider_environment, binding.provider_environment)
            self.assertEqual(selected.network_policy_id, "direct-tls-v1")
            self.assertIs(revalidate.call_args.kwargs["qualification_reader"], reader)
            self.assertIs(revalidate.call_args.kwargs["capability_registry"], registry)
            self.assertIsNotNone(revalidate.call_args.kwargs["at"].tzinfo)

    def test_caller_cannot_self_assert_provider_origin_token(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                selected_authority=selected_authority(query),
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "2" * 64,
                recorded_at=READ_NOW,
            )
            with self.assertRaisesRegex(
                ProviderOriginError, "lacks factory-issued wire execution evidence"
            ):
                journal._record_provider_origin(
                    attempt_id,
                    query,
                    wire_receipt=object(),
                )
            events = JournalStore.load_events(
                journal._store,
                "authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual([event["event_type"] for event in events], [
                "AuthenticatedReadPrepared"
            ])

    def test_generic_journal_writer_cannot_fabricate_provider_origin_history(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            journal = _provider_journal(store)
            attempt_id = "provider-read:generic-forgery"
            snapshot = provider_origin_module._query_snapshot(query)
            payload = {
                "origin_kind": "PROVIDER_ORIGIN",
                "query": snapshot,
                "transport_identity": "ProductDirectWire:v1",
                "network_policy_identity": "sha256:" + "c" * 64,
            }
            envelope = ProviderOriginJournal._event(
                event_id=attempt_id + ":prepared",
                event_type="AuthenticatedReadPrepared",
                attempt_id=attempt_id,
                aggregate_version=1,
                payload=payload,
                committed_at=READ_NOW.isoformat(),
            )
            with self.assertRaisesRegex(
                ValueError,
                "requires protected writer capability",
            ):
                JournalStore.append_event(store, envelope)
            self.assertIsNone(store.get_event(attempt_id + ":prepared"))
            with self.assertRaisesRegex(ProviderOriginError, "incomplete"):
                journal.load_response_binding(attempt_id, query)

    def test_legacy_generic_provider_history_is_not_silently_upgraded(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            attempt_id = "provider-read:legacy-generic"
            payload = {
                "origin_kind": "PROVIDER_ORIGIN",
                "query": provider_origin_module._query_snapshot(query),
                "transport_identity": "ProductDirectWire:v1",
                "network_policy_identity": "sha256:" + "d" * 64,
            }
            JournalStore.append_event(
                store,
                ProviderOriginJournal._event(
                    event_id=attempt_id + ":prepared",
                    event_type="AuthenticatedReadPrepared",
                    attempt_id=attempt_id,
                    aggregate_version=1,
                    payload=payload,
                    committed_at=READ_NOW.isoformat(),
                ),
            )
            with self.assertRaisesRegex(
                ValueError,
                "cannot bless pre-existing aggregate history",
            ):
                _provider_journal(JournalStore(path))

    def test_provider_origin_requires_preissued_writer_with_exact_scope(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            wrong = JournalStore.bind_protected_writer(
                store,
                aggregate_type="not_provider_origin",
                namespace="not-provider-origin:v1",
                writer_authority_id="issuer:test:v1",
            )
            with self.assertRaisesRegex(
                ProviderOriginError,
                "wrong scope",
            ):
                ProviderOriginJournal(
                    store,
                    writer_capability=wrong,
                    response_store=ArtifactStore(
                        Path(store.path).parent / "provider-origin-artifacts"
                    ),
                )

    def test_prepared_only_attempt_cannot_become_response_binding(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                selected_authority=selected_authority(query),
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "3" * 64,
                recorded_at=READ_NOW,
            )
            with self.assertRaisesRegex(ProviderOriginError, "incomplete"):
                journal.load_response_binding(attempt_id, query)

    def test_exact_query_binding_is_revalidated_at_recovery(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                selected_authority=selected_authority(query),
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "4" * 64,
                recorded_at=READ_NOW,
            )
            _record_test_provider_origin(
                journal,
                attempt_id,
                query,
                http_status=200,
                response_bytes=b'{"ok":true}',
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            other = authenticated_read_binding(
                query={"omitZeroBalances": "false"}
            )
            with self.assertRaisesRegex(ProviderOriginError, "does not match"):
                journal.load_response_binding(attempt_id, other)

            object.__setattr__(query, "endpoint", "/api/v3/openOrders")
            with self.assertRaisesRegex(ProviderOriginError, "digest conflicts"):
                journal.load_response_binding(attempt_id, query)

    def test_second_conflicting_observation_cannot_replace_first(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                selected_authority=selected_authority(query),
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "5" * 64,
                recorded_at=READ_NOW,
            )
            first = _record_test_provider_origin(
                journal,
                attempt_id,
                query,
                http_status=200,
                response_bytes=b'{"value":1}',
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            with self.assertRaisesRegex(
                ProviderOriginError, "requires one exact durable Prepared event"
            ):
                _record_test_provider_origin(
                journal,
                    attempt_id,
                    query,
                    http_status=200,
                    response_bytes=b'{"value":2}',
                    observed_at=READ_NOW + timedelta(seconds=2),
                )
            recovered = journal.load_response_binding(
                attempt_id, authenticated_read_binding()
            )
            self.assertEqual(recovered.response_sha256, first.response_sha256)
            self.assertEqual(recovered.response_bytes, b'{"value":1}')

    def test_non_success_provider_origin_is_durable_but_not_financial_observation(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                selected_authority=selected_authority(query),
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "6" * 64,
                recorded_at=READ_NOW,
            )
            binding = _record_test_provider_origin(
                journal,
                attempt_id,
                query,
                http_status=429,
                response_bytes=b'{"code":-1003,"msg":"too many requests"}',
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            self.assertEqual(binding.http_status, 429)
            with self.assertRaisesRegex(
                ProviderOriginError,
                "status is not accepted",
            ):
                _promote_provider_origin(
                    journal=journal,
                    response_binding=binding,
                    query_binding=query,
                required_data_entitlement="ACCOUNT",
                )

    def test_route_specific_success_status_blocks_generic_2xx_promotion(self):
        self.assertEqual(
            BINANCE_SPOT_AUTHENTICATED_READ_ENDPOINTS[
                "/api/v3/account"
            ].success_statuses,
            frozenset({200}),
        )
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                selected_authority=selected_authority(query),
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "c" * 64,
                recorded_at=READ_NOW,
            )
            binding = _record_test_provider_origin(
                journal,
                attempt_id,
                query,
                http_status=201,
                response_bytes=b'{"ok":true}',
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            recovered = journal.load_response_binding(attempt_id, query)
            self.assertEqual(recovered.http_status, 201)
            self.assertEqual(recovered.route_digest, binding.route_digest)
            self.assertEqual(recovered.data_entitlement, "ACCOUNT")
            with self.assertRaisesRegex(
                ProviderOriginError,
                "status is not accepted",
            ):
                _promote_provider_origin(
                    journal=journal,
                    response_binding=recovered,
                    query_binding=query,
                    required_data_entitlement="ACCOUNT",
                )

    def test_financial_consumer_cannot_relabel_route_entitlement(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            attempt_id = journal.prepare(
                query,
                selected_authority=selected_authority(query),
                transport_identity="UrllibJsonWireClient:v1",
                network_policy_identity="sha256:" + "d" * 64,
                recorded_at=READ_NOW,
            )
            binding = _record_test_provider_origin(
                journal,
                attempt_id,
                query,
                http_status=200,
                response_bytes=b'{"ok":true}',
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            with self.assertRaisesRegex(
                ProviderOriginError,
                "entitlement does not match consumer",
            ):
                _promote_provider_origin(
                    journal=journal,
                    response_binding=binding,
                    query_binding=query,
                    required_data_entitlement="ORDERS",
                )

    def test_provider_origin_authority_values_cannot_be_subclassed(self):
        with self.assertRaisesRegex(
            TypeError, "AuthenticatedReadResponseBinding must not be subclassed"
        ):
            class ForgedResponseBinding(AuthenticatedReadResponseBinding):
                def __post_init__(self, _binding_token=None):
                    pass

        with self.assertRaisesRegex(
            TypeError, "ProviderOriginObservation must not be subclassed"
        ):
            class ForgedOriginObservation(ProviderOriginObservation):
                def __post_init__(self, _observation_token=None):
                    pass


    def test_response_binding_cannot_be_publicly_constructed(self):
        with self.assertRaisesRegex(
            ProviderOriginError, "must come from durable origin journal"
        ):
            AuthenticatedReadResponseBinding(
                attempt_id="provider-read:" + "a" * 32,
                provider_id="BINANCE",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="PAPER",
                capability_snapshot_id="cap-1",
                query_digest="sha256:" + "a" * 64,
                endpoint="/api/v3/account",
                route_digest="sha256:" + "e" * 64,
                data_entitlement="ACCOUNT",
                selection_identity="sha256:" + "f" * 64,
                product_family="SPOT",
                adapter_code_sha="1" * 40,
                qualification_id="qualification:test:v1",
                route_policy_id="binance-qualified-route-v1",
                entity_policy_id="binance-entity-v1",
                qualification_network_policy_id="direct-tls-v1",
                account_class="STANDARD",
                release_artifact_id=None,
                release_artifact_sha256=None,
                reconciliation_semantics_id=None,
                transport_identity="ProductDirectWire:v1",
                network_policy_identity="sha256:" + "d" * 64,
                prepared_event_id="prepared",
                observed_event_id="observed",
                observed_at="2026-09-25T10:00:01Z",
                http_status=200,
                response_sha256="sha256:" + "b" * 64,
                response_artifact_id="00000000-0000-5000-8000-000000000000",
                response_bytes=b"{}",
                origin_ref="provider-origin:sha256:" + "c" * 64,
                journal_sequence=2,
            )



    def test_durable_selection_record_binds_q_build_and_exact_query_scope(self):
        query = authenticated_read_binding()
        selected = selected_authority(query)
        record = provider_origin_module._selection_record(selected, query)

        self.assertEqual(record["qualification_id"], selected.qualification_id)
        self.assertEqual(record["adapter_code_sha"], selected.adapter_code_sha)
        self.assertEqual(
            record["capability_snapshot_id"],
            query.capability_snapshot_id,
        )
        self.assertTrue(record["selection_identity"].startswith("sha256:"))

        tampered = dict(record)
        tampered["qualification_id"] = "qualification:other:v1"
        with self.assertRaisesRegex(
            ProviderOriginError,
            "selected provider authority identity is invalid",
        ):
            provider_origin_module._require_stored_selection(tampered, query)

    def test_prepare_rejects_selection_for_another_capability(self):
        query = authenticated_read_binding()
        selected = selected_authority(query)
        object.__setattr__(
            selected,
            "capability_snapshot_id",
            "different-capability",
        )
        with TemporaryDirectory() as directory:
            journal = _provider_journal(
                JournalStore(f"{directory}/journal.sqlite3")
            )
            with self.assertRaisesRegex(
                ProviderOriginError,
                "conflicts with exact query scope",
            ):
                journal.prepare(
                    query,
                    selected_authority=selected,
                    transport_identity="UrllibJsonWireClient:v1",
                    network_policy_identity="sha256:" + "7" * 64,
                    recorded_at=READ_NOW,
                )

    @staticmethod
    def _rewrite_event_payload(path, event_id, mutate):
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT payload_json, envelope_json FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise AssertionError("test event is missing")
            payload = json.loads(row[0])
            mutate(payload)
            payload_json = canonical_json(payload)
            payload_hash = payload_digest(payload)
            envelope = json.loads(row[1])
            envelope["payload"] = payload
            envelope["payload_hash"] = payload_hash
            envelope_json = canonical_json(envelope)
            envelope_hash = "sha256:" + sha256(
                envelope_json.encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                UPDATE events
                SET payload_json = ?, payload_hash = ?,
                    envelope_json = ?, envelope_hash = ?
                WHERE event_id = ?
                """,
                (
                    payload_json,
                    payload_hash,
                    envelope_json,
                    envelope_hash,
                    event_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _durable_origin_fixture(
        self,
        directory,
        *,
        transport_identity="UrllibJsonWireClient:v1",
        network_policy_identity="sha256:" + "7" * 64,
        http_status=200,
        body=b'{"ok":true}',
        observed_at=None,
    ):
        path = f"{directory}/journal.sqlite3"
        query = authenticated_read_binding()
        journal = _provider_journal(JournalStore(path))
        attempt_id = journal.prepare(
            query,
            selected_authority=selected_authority(query),
            transport_identity=transport_identity,
            network_policy_identity=network_policy_identity,
            recorded_at=READ_NOW,
        )
        binding = _record_test_provider_origin(
                journal,
            attempt_id,
            query,
            http_status=http_status,
            response_bytes=body,
            observed_at=(
                READ_NOW + timedelta(seconds=1)
                if observed_at is None
                else observed_at
            ),
        )
        return path, query, attempt_id, binding

    def test_origin_identity_binds_exact_http_status(self):
        with TemporaryDirectory() as first_directory, TemporaryDirectory() as second_directory:
            _, _, _, original = self._durable_origin_fixture(
                first_directory,
                http_status=200,
            )
            _, _, _, changed = self._durable_origin_fixture(
                second_directory,
                http_status=403,
            )
            self.assertEqual(changed.http_status, 403)
            self.assertNotEqual(
                changed.origin_ref,
                original.origin_ref,
                "HTTP status is part of provider-origin semantic identity",
            )

    def test_origin_identity_binds_exact_observed_instant(self):
        with TemporaryDirectory() as first_directory, TemporaryDirectory() as second_directory:
            _, _, _, first = self._durable_origin_fixture(
                first_directory,
                observed_at=READ_NOW + timedelta(seconds=1),
            )
            _, _, _, second = self._durable_origin_fixture(
                second_directory,
                observed_at=READ_NOW + timedelta(seconds=2),
            )
            self.assertNotEqual(first.observed_at, second.observed_at)
            self.assertNotEqual(
                first.origin_ref,
                second.origin_ref,
                "observed instant is part of canonical provider-origin identity",
            )

    def test_origin_identity_binds_transport_but_ignores_caller_network_claim(self):
        with TemporaryDirectory() as first_directory, TemporaryDirectory() as second_directory:
            _, query, _, original = self._durable_origin_fixture(
                first_directory,
                transport_identity="ProductDirectWire:v1",
                network_policy_identity="sha256:" + "8" * 64,
            )
            _, _, _, same_authority = self._durable_origin_fixture(
                second_directory,
                transport_identity="ProductDirectWire:v1",
                network_policy_identity="sha256:" + "9" * 64,
            )
            canonical_policy = canonical_authenticated_read_route(query)[
                "network_policy_identity"
            ]
            self.assertEqual(
                original.network_policy_identity,
                canonical_policy,
            )
            self.assertEqual(
                same_authority.network_policy_identity,
                canonical_policy,
            )
            self.assertEqual(
                same_authority.origin_ref,
                original.origin_ref,
                "caller compatibility network claims must not redefine origin authority",
            )

        with TemporaryDirectory() as first_directory, TemporaryDirectory() as second_directory:
            _, _, _, first_transport = self._durable_origin_fixture(
                first_directory,
                transport_identity="ProductDirectWire:v1",
            )
            _, _, _, changed_transport = self._durable_origin_fixture(
                second_directory,
                transport_identity="ProductDirectWire:v2",
            )
            self.assertNotEqual(
                changed_transport.origin_ref,
                first_transport.origin_ref,
                "canonical transport identity remains part of provider-origin identity",
            )

    def test_loaded_binding_retains_canonical_route_network_authority(self):
        transport = "ProductDirectWire:v1"
        caller_policy = "sha256:" + "a" * 64
        with TemporaryDirectory() as directory:
            _, query, _, binding = self._durable_origin_fixture(
                directory,
                transport_identity=transport,
                network_policy_identity=caller_policy,
            )
            canonical_policy = canonical_authenticated_read_route(query)[
                "network_policy_identity"
            ]
            self.assertEqual(
                getattr(binding, "transport_identity", None),
                transport,
            )
            self.assertEqual(
                getattr(binding, "network_policy_identity", None),
                canonical_policy,
            )
            self.assertNotEqual(
                binding.network_policy_identity,
                caller_policy,
            )

    def test_prepared_network_policy_is_derived_from_canonical_route(self):
        query = authenticated_read_binding()
        caller_policy = "sha256:" + "e" * 64
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = _provider_journal(JournalStore(path))
            attempt_id = journal.prepare(
                query,
                selected_authority=selected_authority(query),
                transport_identity="ProductDirectWire:v1",
                network_policy_identity=caller_policy,
                recorded_at=READ_NOW,
            )
            prepared = journal._load_protected_history(
                journal._store,
                attempt_id,
            )[0]["payload"]
            canonical_route = dict(canonical_authenticated_read_route(query))
            self.assertEqual(
                prepared["network_policy_identity"],
                canonical_route["network_policy_identity"],
            )
            self.assertEqual(
                prepared["route"]["network_policy_identity"],
                canonical_route["network_policy_identity"],
            )
            self.assertNotEqual(
                prepared["network_policy_identity"],
                caller_policy,
            )

    def test_rehashed_prepared_evidence_class_tamper_fails_closed(self):
        with TemporaryDirectory() as directory:
            path, query, attempt_id, _ = self._durable_origin_fixture(directory)
            self._rewrite_event_payload(
                path,
                attempt_id + ":prepared",
                lambda payload: payload.__setitem__(
                    "origin_kind", "TEST_INJECTED"
                ),
            )
            with self.assertRaises(ProviderOriginError):
                _provider_journal(JournalStore(path)).load_response_binding(
                    attempt_id, query
                )

    def test_rehashed_unknown_prepared_authority_field_fails_closed(self):
        with TemporaryDirectory() as directory:
            path, query, attempt_id, _ = self._durable_origin_fixture(directory)
            self._rewrite_event_payload(
                path,
                attempt_id + ":prepared",
                lambda payload: payload.__setitem__(
                    "future_authority", "caller-selected"
                ),
            )
            with self.assertRaises(ProviderOriginError):
                _provider_journal(JournalStore(path)).load_response_binding(
                    attempt_id, query
                )

    def test_rehashed_invalid_wire_authority_fields_fail_closed(self):
        with TemporaryDirectory() as directory:
            path, query, attempt_id, _ = self._durable_origin_fixture(directory)

            def mutate(payload):
                payload["transport_identity"] = ""
                payload["network_policy_identity"] = "not-a-policy-digest"

            self._rewrite_event_payload(
                path, attempt_id + ":prepared", mutate
            )
            self._rewrite_event_payload(
                path, attempt_id + ":observed", mutate
            )
            with self.assertRaises(ProviderOriginError):
                _provider_journal(JournalStore(path)).load_response_binding(
                    attempt_id, query
                )

    def test_observed_commit_failure_recovers_without_wire_or_caller_bytes(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = _provider_journal(JournalStore(path))
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

            with patch.object(
                JournalStore,
                "append_protected_event",
                new=fail_observed,
            ):
                attempt_id = journal.prepare(
                    query,
                    selected_authority=selected_authority(query),
                    transport_identity="UrllibJsonWireClient:v1",
                    network_policy_identity="sha256:" + "b" * 64,
                    recorded_at=READ_NOW,
                )
                with self.assertRaisesRegex(
                    RuntimeError, "forced observed commit failure"
                ):
                    _record_test_provider_origin(
                        journal,
                        attempt_id,
                        query,
                        http_status=200,
                        response_bytes=b'{"ok":true}',
                        observed_at=READ_NOW + timedelta(seconds=1),
                    )

            events = JournalStore.load_events(
                journal._store,
                "authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "AuthenticatedReadPrepared",
                    "AuthenticatedReadResponseRetained",
                ],
            )
            with self.assertRaisesRegex(ProviderOriginError, "incomplete"):
                journal.load_response_binding(attempt_id, query)

            # Model restart.  Recovery receives no receipt/wire, response bytes,
            # HTTP status, or observation time from the caller.
            restarted = _provider_journal(JournalStore(path))
            with patch(
                "mvp.autotrade_mvp.provider_transport.execute_product_authenticated_read",
                side_effect=AssertionError("network re-query forbidden"),
            ) as execute:
                recovered = restarted.recover_response_binding(
                    attempt_id,
                    query,
                )
            execute.assert_not_called()
            self.assertEqual(recovered.response_bytes, b'{"ok":true}')
            self.assertEqual(recovered.http_status, 200)
            self.assertEqual(
                restarted._response_store.audit().manifests,
                1,
            )
            final_events = JournalStore.load_events(
                restarted._store,
                "authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(
                [event["event_type"] for event in final_events],
                [
                    "AuthenticatedReadPrepared",
                    "AuthenticatedReadResponseRetained",
                    "AuthenticatedReadObserved",
                ],
            )

    def test_retained_recovery_fails_closed_when_response_artifact_is_unreadable(self):
        query = authenticated_read_binding()
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            journal = _provider_journal(JournalStore(path))
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

            with patch.object(
                JournalStore,
                "append_protected_event",
                new=fail_observed,
            ):
                attempt_id = journal.prepare(
                    query,
                    selected_authority=selected_authority(query),
                    transport_identity="UrllibJsonWireClient:v1",
                    network_policy_identity="sha256:" + "b" * 64,
                    recorded_at=READ_NOW,
                )
                with self.assertRaisesRegex(RuntimeError, "forced observed"):
                    _record_test_provider_origin(
                        journal,
                        attempt_id,
                        query,
                        http_status=200,
                        response_bytes=b'{"ok":true}',
                        observed_at=READ_NOW + timedelta(seconds=1),
                    )

            restarted = _provider_journal(JournalStore(path))
            with patch.object(
                restarted,
                "_response_reader",
                side_effect=OSError("retained bytes unavailable"),
            ):
                with self.assertRaisesRegex(
                    ProviderOriginError,
                    "cannot be authenticated",
                ):
                    restarted.recover_response_binding(attempt_id, query)
            events = JournalStore.load_events(
                restarted._store,
                "authenticated_provider_read",
                attempt_id,
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "AuthenticatedReadPrepared",
                    "AuthenticatedReadResponseRetained",
                ],
            )


if __name__ == "__main__":
    unittest.main()
