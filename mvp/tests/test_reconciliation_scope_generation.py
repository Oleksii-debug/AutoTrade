from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from autotrade_mvp.persistence import (
    JournalSequencePreconditionFailed,
    JournalStore,
    payload_digest,
)
from autotrade_mvp.provider_selection import SelectedProviderAuthority
from autotrade_mvp.reconciliation_journal import (
    prepare_reconciliation_scope_generation,
    reconciliation_scope_generation_payload,
    load_reconciliation_scope_generation_payload,
    require_current_reconciliation_scope_generation,
    require_current_reconciliation_scope_generation_payload,
)


def _selected(*, provider_environment: str = "TESTNET") -> SelectedProviderAuthority:
    return SelectedProviderAuthority(
        provider_id="BYBIT",
        product_family="SPOT",
        adapter_code_sha="1" * 40,
        qualification_id="sha256:" + "2" * 64,
        capability_snapshot_id=str(UUID(int=1)),
        account_id="acct-1",
        entity_id="bybit-global",
        environment="PAPER",
        provider_environment=provider_environment,
        instrument_version="BTCUSDT:1",
        route_policy_id="route:v1",
        entity_policy_id="entity:v1",
        network_policy_id="network:v1",
        account_class="UNIFIED",
        release_artifact_id=None,
        release_artifact_sha256=None,
        reconciliation_semantics_id="sha256:" + "3" * 64,
    )


def _prepare(
    store: JournalStore,
    selected: SelectedProviderAuthority,
    request_id: str,
):
    return prepare_reconciliation_scope_generation(
        store,
        selected_authority=selected,
        acquisition_request_id=request_id,
        host_id="host-1",
        owner_epoch="epoch-1",
    )


def _unrelated_envelope() -> dict[str, object]:
    event_id = str(uuid4())
    payload = {"kind": "race"}
    instant = "2026-10-01T00:00:00Z"
    return {
        "event_id": event_id,
        "event_type": "ReconciliationScopeRaceFixture",
        "schema_version": "1.0.0",
        "aggregate_type": "reconciliation_scope_race_fixture",
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


class ReconciliationScopeGenerationTests(unittest.TestCase):
    def _store(self, directory: str) -> JournalStore:
        return JournalStore(Path(directory) / "journal.sqlite3")

    def test_exact_prepare_retry_is_idempotent_while_current(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            request_id = str(UUID(int=10))
            first = _prepare(store, _selected(), request_id)
            retry = _prepare(store, _selected(), request_id)

            self.assertEqual(retry, first)
            self.assertEqual(first.generation, 1)
            self.assertEqual(first.aggregate_version, 1)
            self.assertEqual(
                require_current_reconciliation_scope_generation(store, first),
                first,
            )
            events = JournalStore.load_events(
                store,
                "reconciliation_scope",
                first.aggregate_id,
            )
            self.assertEqual(len(events), 1)

    def test_new_prepare_supersedes_old_generation_and_old_retry(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            first_id = str(UUID(int=11))
            second_id = str(UUID(int=12))
            first = _prepare(store, _selected(), first_id)
            second = _prepare(store, _selected(), second_id)

            self.assertEqual(second.aggregate_id, first.aggregate_id)
            self.assertEqual(second.generation, 2)
            self.assertEqual(second.aggregate_version, 2)
            with self.assertRaisesRegex(ValueError, "superseded"):
                require_current_reconciliation_scope_generation(store, first)
            self.assertEqual(
                require_current_reconciliation_scope_generation(store, second),
                second,
            )
            with self.assertRaisesRegex(ValueError, "superseded generation"):
                _prepare(store, _selected(), first_id)

    def test_same_request_id_cannot_change_selected_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            request_id = str(UUID(int=13))
            original = _selected()
            _prepare(store, original, request_id)
            changed = replace(
                original,
                qualification_id="sha256:" + "4" * 64,
            )

            with self.assertRaisesRegex(ValueError, "different authority"):
                _prepare(store, changed, request_id)

    def test_provider_environment_has_distinct_scope_generation_lane(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            request_id = str(UUID(int=14))
            testnet = _prepare(store, _selected(provider_environment="TESTNET"), request_id)
            demo = _prepare(store, _selected(provider_environment="DEMO"), request_id)

            self.assertNotEqual(testnet.aggregate_id, demo.aggregate_id)
            self.assertEqual(testnet.generation, 1)
            self.assertEqual(demo.generation, 1)
            self.assertEqual(testnet.provider_environment, "TESTNET")
            self.assertEqual(demo.provider_environment, "DEMO")

    def test_prepare_requires_reconciliation_semantics(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            missing = replace(_selected(), reconciliation_semantics_id=None)

            with self.assertRaisesRegex(ValueError, "lacks reconciliation semantics"):
                _prepare(store, missing, str(UUID(int=15)))
            self.assertEqual(
                JournalStore.load_events_by_aggregate_type(
                    store,
                    "reconciliation_scope",
                ),
                [],
            )

    def test_global_journal_cut_race_fails_before_scope_prepare(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            original_load_events = JournalStore.load_events
            raced = False

            def raced_load_events(
                current: JournalStore,
                aggregate_type: str,
                aggregate_id: str,
            ):
                nonlocal raced
                if not raced:
                    raced = True
                    JournalStore.append_event(current, _unrelated_envelope())
                return original_load_events(current, aggregate_type, aggregate_id)

            with patch.object(
                JournalStore,
                "load_events",
                new=raced_load_events,
            ):
                with self.assertRaises(JournalSequencePreconditionFailed):
                    _prepare(store, _selected(), str(UUID(int=16)))

            self.assertTrue(raced)
            self.assertEqual(
                JournalStore.load_events_by_aggregate_type(
                    store,
                    "reconciliation_scope",
                ),
                [],
            )


    def test_serialized_generation_round_trips_only_while_current(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            first = _prepare(store, _selected(), str(UUID(int=21)))
            payload = reconciliation_scope_generation_payload(first)

            resolved = require_current_reconciliation_scope_generation_payload(
                store,
                payload,
            )
            self.assertEqual(resolved, first)

            _prepare(store, _selected(), str(UUID(int=22)))
            self.assertEqual(
                load_reconciliation_scope_generation_payload(store, payload),
                first,
            )
            with self.assertRaisesRegex(ValueError, "superseded"):
                require_current_reconciliation_scope_generation_payload(
                    store,
                    payload,
                )

    def test_serialized_generation_rejects_tampered_provider_domain(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            current = _prepare(store, _selected(), str(UUID(int=23)))
            payload = reconciliation_scope_generation_payload(current)
            payload["provider_environment"] = "DEMO"

            with self.assertRaisesRegex(
                ValueError,
                "differs from durable authority",
            ):
                require_current_reconciliation_scope_generation_payload(
                    store,
                    payload,
                )

    def test_serialized_generation_shape_is_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            current = _prepare(store, _selected(), str(UUID(int=24)))
            payload = reconciliation_scope_generation_payload(current)
            payload["caller_extension"] = "not-authority"

            with self.assertRaisesRegex(ValueError, "shape is non-canonical"):
                require_current_reconciliation_scope_generation_payload(
                    store,
                    payload,
                )


if __name__ == "__main__":
    unittest.main()
