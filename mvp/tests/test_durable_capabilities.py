from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilityError,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore


NOW = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)


def claim(source: str, *, observed_at: datetime) -> CapabilityClaim:
    return CapabilityClaim(
        source=source,
        provider_id="simulated",
        account_id="paper-account",
        entity_id="entity-1",
        environment="PAPER",
        instrument_version="instrument-v1",
        observed_at=observed_at,
        expires_at=observed_at + timedelta(minutes=10),
        supported_order_types=frozenset({"LIMIT"}),
        time_in_force=frozenset({"DAY"}),
        permission_scopes=frozenset({"ORDER.READ", "ORDER.WRITE"}),
        position_mode="NET",
        native_protection=frozenset({"STOP_LOSS"}),
        rate_limit_policy_id="paper-rate-v1",
        data_entitlements=frozenset({"QUOTE"}),
        evidence_ref={
            "artifact_id": {
                "DOCUMENTED": "11111111-1111-4111-8111-111111111111",
                "API": "22222222-2222-4222-8222-222222222222",
                "ACCOUNT": "33333333-3333-4333-8333-333333333333",
                "INSTRUMENT": "44444444-4444-4444-8444-444444444444",
            }[source],
            "sha256": "sha256:" + {
                "DOCUMENTED": "1",
                "API": "2",
                "ACCOUNT": "3",
                "INSTRUMENT": "4",
            }[source] * 64,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        },
    )


def verified(snapshot_id: str, observed_at: datetime):
    return derive_capability_snapshot(
        snapshot_id=snapshot_id,
        claims=tuple(
            claim(source, observed_at=observed_at)
            for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
        ),
        observed_at=observed_at,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


class DurableCapabilityRegistryTests(unittest.TestCase):
    def test_history_survives_restart_but_verified_authority_requires_refresh(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first = DurableCapabilityRegistry(JournalStore(path))
            snapshot = verified(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                NOW,
            )
            self.assertTrue(first.add(snapshot))
            self.assertEqual(
                first.require_verified(
                    provider_id="simulated",
                    account_id="paper-account",
                    entity_id="entity-1",
                    environment="PAPER",
                    instrument_version="instrument-v1",
                    at=NOW + timedelta(seconds=1),
                ).snapshot_id,
                snapshot.snapshot_id,
            )

            restarted = DurableCapabilityRegistry(JournalStore(path))
            historical = restarted.latest(
                provider_id="simulated",
                account_id="paper-account",
                entity_id="entity-1",
                environment="PAPER",
                instrument_version="instrument-v1",
                at=NOW + timedelta(seconds=1),
            )
            self.assertEqual(historical, snapshot)
            self.assertFalse(
                historical.admits(
                    at=NOW + timedelta(seconds=1),
                    order_type="LIMIT",
                    time_in_force="DAY",
                    permission_scope="ORDER.WRITE",
                )
            )
            with self.assertRaisesRegex(
                CapabilityError,
                "fresh current-process verification",
            ):
                restarted.require_verified(
                    provider_id="simulated",
                    account_id="paper-account",
                    entity_id="entity-1",
                    environment="PAPER",
                    instrument_version="instrument-v1",
                    at=NOW + timedelta(seconds=1),
                )

            refreshed_at = NOW + timedelta(minutes=1)
            refreshed = verified(
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                refreshed_at,
            )
            self.assertTrue(restarted.add(refreshed))
            admitted = restarted.require_verified(
                provider_id="simulated",
                account_id="paper-account",
                entity_id="entity-1",
                environment="PAPER",
                instrument_version="instrument-v1",
                at=refreshed_at + timedelta(seconds=1),
            )
            self.assertEqual(admitted.snapshot_id, refreshed.snapshot_id)
            self.assertTrue(
                admitted.admits(
                    at=refreshed_at + timedelta(seconds=1),
                    order_type="LIMIT",
                    time_in_force="DAY",
                    permission_scope="ORDER.WRITE",
                )
            )

    def test_newer_unknown_refresh_persists_and_supersedes_verified_history(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            registry = DurableCapabilityRegistry(JournalStore(path))
            old = verified(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                NOW,
            )
            registry.add(old)

            observed = NOW + timedelta(minutes=1)
            unknown = derive_capability_snapshot(
                snapshot_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                claims=tuple(
                    claim(source, observed_at=observed)
                    for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
                ),
                observed_at=observed,
            )
            self.assertEqual(unknown.status, "UNKNOWN")
            self.assertTrue(registry.add(unknown))

            restarted = DurableCapabilityRegistry(JournalStore(path))
            latest = restarted.latest(
                provider_id="simulated",
                account_id="paper-account",
                entity_id="entity-1",
                environment="PAPER",
                instrument_version="instrument-v1",
                at=observed + timedelta(seconds=1),
            )
            self.assertEqual(latest.snapshot_id, unknown.snapshot_id)
            self.assertEqual(latest.status, "UNKNOWN")
            with self.assertRaisesRegex(CapabilityError, "status is UNKNOWN"):
                restarted.require_verified(
                    provider_id="simulated",
                    account_id="paper-account",
                    entity_id="entity-1",
                    environment="PAPER",
                    instrument_version="instrument-v1",
                    at=observed + timedelta(seconds=1),
                )

    def test_tampered_durable_snapshot_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            registry = DurableCapabilityRegistry(JournalStore(path))
            registry.add(
                verified(
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    NOW,
                )
            )
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE events SET payload_json=? "
                    "WHERE aggregate_type='capability_history'",
                    ('{"schema_version":"1.0.0","snapshot":{},"sources":[]}',),
                )
                connection.commit()
            with self.assertRaisesRegex(ValueError, "payload hash|envelope"):
                DurableCapabilityRegistry(JournalStore(path)).latest(
                    provider_id="simulated",
                    account_id="paper-account",
                    entity_id="entity-1",
                    environment="PAPER",
                    instrument_version="instrument-v1",
                    at=NOW + timedelta(seconds=1),
                )

    def test_foreign_event_type_in_capability_aggregate_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            registry = DurableCapabilityRegistry(JournalStore(path))
            snapshot = verified(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                NOW,
            )
            registry.add(snapshot)
            event = registry.store.load_events_by_aggregate_type(
                "capability_history"
            )[0]
            registry.store.append_event(
                {
                    "event_id": "foreign-capability-event",
                    "event_type": "ForeignCapabilityEvent.v1",
                    "aggregate_type": event["aggregate_type"],
                    "aggregate_id": event["aggregate_id"],
                    "aggregate_version": "2",
                    "payload": event["payload"],
                    "payload_hash": event["payload_hash"],
                    "committed_at": (NOW + timedelta(seconds=1)).isoformat(),
                }
            )
            with self.assertRaisesRegex(
                CapabilityError,
                "unsupported durable capability event type",
            ):
                DurableCapabilityRegistry(JournalStore(path)).latest(
                    provider_id="simulated",
                    account_id="paper-account",
                    entity_id="entity-1",
                    environment="PAPER",
                    instrument_version="instrument-v1",
                    at=NOW + timedelta(seconds=1),
                )

    def test_stale_writer_cannot_append_older_snapshot(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first = DurableCapabilityRegistry(JournalStore(path))
            stale = DurableCapabilityRegistry(JournalStore(path))
            first.add(
                verified(
                    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    NOW + timedelta(minutes=1),
                )
            )
            with self.assertRaisesRegex(CapabilityError, "observed_at must advance"):
                stale.add(
                    verified(
                        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        NOW,
                    )
                )


if __name__ == "__main__":
    unittest.main()
