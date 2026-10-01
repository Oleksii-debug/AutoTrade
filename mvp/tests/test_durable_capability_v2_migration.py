from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilityError,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json, payload_digest


NOW = datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)
V1_EVENT = "CapabilitySnapshotObserved.v1"
V2_EVENT = "CapabilitySnapshotObserved.v2"


def claim(
    source: str,
    *,
    provider_id: str,
    observed_at: datetime,
    provider_environment: str | None,
) -> CapabilityClaim:
    return CapabilityClaim(
        source=source,
        provider_id=provider_id,
        account_id="paper-account",
        entity_id="entity-1",
        environment="PAPER",
        instrument_version="instrument-v1",
        observed_at=observed_at,
        expires_at=observed_at + timedelta(minutes=10),
        supported_order_types=frozenset({"LIMIT"}),
        time_in_force=frozenset({"GTC"}),
        permission_scopes=frozenset({"ORDER.READ"}),
        position_mode="NET",
        native_protection=frozenset({"STOP"}),
        rate_limit_policy_id="rate-v1",
        data_entitlements=frozenset({"QUOTE"}),
        evidence_ref={
            "artifact_id": str(uuid4()),
            "sha256": "sha256:" + {
                "DOCUMENTED": "1",
                "API": "2",
                "ACCOUNT": "3",
                "INSTRUMENT": "4",
            }[source] * 64,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        },
        provider_environment=provider_environment,
    )


def verified(
    *,
    provider_id: str,
    observed_at: datetime,
    provider_environment: str | None,
):
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=tuple(
            claim(
                source,
                provider_id=provider_id,
                observed_at=observed_at,
                provider_environment=provider_environment,
            )
            for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
        ),
        observed_at=observed_at,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def legacy_aggregate_id(snapshot) -> str:
    material = canonical_json(
        [
            snapshot.provider_id,
            snapshot.account_id,
            snapshot.entity_id,
            snapshot.environment,
            snapshot.instrument_version,
        ]
    ).encode("utf-8")
    return "capability:" + sha256(material).hexdigest()


def append_legacy_v1(store: JournalStore, snapshot) -> None:
    raw = snapshot.to_contract_dict()
    raw.pop("provider_environment")
    payload = {
        "schema_version": "1.0.0",
        "snapshot": raw,
        "sources": sorted(snapshot.sources),
    }
    store.append_event(
        {
            "event_id": "legacy-capability:" + snapshot.snapshot_id,
            "event_type": V1_EVENT,
            "aggregate_type": "capability_history",
            "aggregate_id": legacy_aggregate_id(snapshot),
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": snapshot.observed_at.isoformat(),
        }
    )


class DurableCapabilityV2MigrationTests(unittest.TestCase):
    def test_non_bybit_v1_replays_as_runtime_domain_then_advances_to_v2(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            old = verified(
                provider_id="SIMULATED",
                observed_at=NOW,
                provider_environment=None,
            )
            append_legacy_v1(store, old)

            registry = DurableCapabilityRegistry(store)
            historical = registry.latest(
                provider_id="SIMULATED",
                account_id="paper-account",
                entity_id="entity-1",
                environment="PAPER",
                provider_environment=None,
                instrument_version="instrument-v1",
                at=NOW + timedelta(seconds=1),
            )
            self.assertEqual(historical.snapshot_id, old.snapshot_id)
            self.assertEqual(historical.provider_environment, "PAPER")

            newer = verified(
                provider_id="SIMULATED",
                observed_at=NOW + timedelta(minutes=1),
                provider_environment=None,
            )
            self.assertTrue(registry.add(newer))
            events = store.load_events_by_aggregate_type("capability_history")
            self.assertEqual(
                [event["event_type"] for event in events],
                [V1_EVENT, V2_EVENT],
            )
            self.assertNotEqual(events[0]["aggregate_id"], events[1]["aggregate_id"])

    def test_legacy_bybit_is_quarantined_until_explicit_later_requalification(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            legacy = verified(
                provider_id="BYBIT",
                observed_at=NOW,
                provider_environment="TESTNET",
            )
            append_legacy_v1(store, legacy)

            registry = DurableCapabilityRegistry(store)
            with self.assertRaisesRegex(CapabilityError, "no capability snapshot"):
                registry.latest(
                    provider_id="BYBIT",
                    account_id="paper-account",
                    entity_id="entity-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    instrument_version="instrument-v1",
                    at=NOW + timedelta(seconds=1),
                )

            stale = verified(
                provider_id="BYBIT",
                observed_at=NOW,
                provider_environment="TESTNET",
            )
            with self.assertRaisesRegex(
                CapabilityError,
                "must advance beyond ambiguous legacy history",
            ):
                registry.add(stale)
            self.assertEqual(
                len(store.load_events_by_aggregate_type("capability_history")),
                1,
            )

            testnet = verified(
                provider_id="BYBIT",
                observed_at=NOW + timedelta(minutes=1),
                provider_environment="TESTNET",
            )
            demo = verified(
                provider_id="BYBIT",
                observed_at=NOW + timedelta(minutes=2),
                provider_environment="DEMO",
            )
            self.assertTrue(registry.add(testnet))
            self.assertTrue(registry.add(demo))
            self.assertNotEqual(testnet.identity, demo.identity)
            self.assertEqual(
                registry.latest(
                    provider_id="BYBIT",
                    account_id="paper-account",
                    entity_id="entity-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    instrument_version="instrument-v1",
                    at=NOW + timedelta(minutes=3),
                ).snapshot_id,
                testnet.snapshot_id,
            )
            self.assertEqual(
                registry.latest(
                    provider_id="BYBIT",
                    account_id="paper-account",
                    entity_id="entity-1",
                    environment="PAPER",
                    provider_environment="DEMO",
                    instrument_version="instrument-v1",
                    at=NOW + timedelta(minutes=3),
                ).snapshot_id,
                demo.snapshot_id,
            )

    def test_lowercase_legacy_bybit_is_quarantined_without_rekeying(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            legacy = verified(
                provider_id="bybit",
                observed_at=NOW,
                provider_environment="TESTNET",
            )
            append_legacy_v1(store, legacy)
            original_id = legacy_aggregate_id(legacy)

            registry = DurableCapabilityRegistry(store)
            with self.assertRaisesRegex(CapabilityError, "no capability snapshot"):
                registry.latest(
                    provider_id="bybit",
                    account_id="paper-account",
                    entity_id="entity-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    instrument_version="instrument-v1",
                    at=NOW + timedelta(seconds=1),
                )

            newer = verified(
                provider_id="bybit",
                observed_at=NOW + timedelta(minutes=1),
                provider_environment="TESTNET",
            )
            self.assertTrue(registry.add(newer))
            events = store.load_events_by_aggregate_type("capability_history")
            self.assertEqual(events[0]["aggregate_id"], original_id)
            self.assertNotEqual(events[1]["aggregate_id"], original_id)

    def test_registry_restart_reuses_exact_writer_generation_fence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first = DurableCapabilityRegistry(JournalStore(path))
            second = DurableCapabilityRegistry(JournalStore(path))
            self.assertIsNot(first, second)

            connection = __import__("sqlite3").connect(path)
            try:
                rows = connection.execute(
                    "SELECT event_type, retirement_id FROM retired_event_types"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(
                rows,
                [(V1_EVENT, "capability-history-v2")],
            )


if __name__ == "__main__":
    unittest.main()
