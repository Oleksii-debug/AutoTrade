from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import risk_policy_authority as authority
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.risk import RiskPolicy
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyAuthorityError,
    RiskPolicyIdentity,
    RiskPolicyScope,
    risk_policy_digest,
    risk_policy_payload,
)


NOW = datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)


def _scope() -> RiskPolicyScope:
    return RiskPolicyScope(
        provider_id="BYBIT",
        account_id="account-1",
        environment="PAPER",
        provider_environment="TESTNET",
        entity_policy_id="bybit-global-v1",
        instrument_family="PERPETUAL",
    )


def _policy(leverage: str) -> RiskPolicy:
    return RiskPolicy.create(
        max_abs_position="100",
        max_single_notional="1000",
        max_gross_leverage=leverage,
        max_net_leverage="1.5",
        max_daily_loss="100",
        max_drawdown_fraction="0.20",
        max_data_age_seconds="5",
        max_fx_age_seconds="5",
        min_margin_headroom="0.10",
        max_stress_loss="200",
    )


class RiskPolicyDowngradeFenceTests(unittest.TestCase):
    def test_switching_lineage_cannot_hide_rollback_of_previously_activated_version(self):
        with TemporaryDirectory() as directory:
            registry = DurableRiskPolicyRegistry(
                JournalStore(Path(directory) / "journal.sqlite3")
            )
            exact_scope = _scope()
            registry.register(
                scope=exact_scope,
                policy_id="core",
                version=1,
                policy=_policy("2.0"),
                committed_at=NOW,
            )
            registry.register(
                scope=exact_scope,
                policy_id="core",
                version=2,
                policy=_policy("1.5"),
                committed_at=NOW + timedelta(seconds=1),
            )
            registry.register(
                scope=exact_scope,
                policy_id="emergency",
                version=1,
                policy=_policy("1.0"),
                committed_at=NOW + timedelta(seconds=2),
            )
            registry.activate(
                scope=exact_scope,
                policy_id="core",
                version=2,
                committed_at=NOW + timedelta(seconds=3),
            )
            registry.activate(
                scope=exact_scope,
                policy_id="emergency",
                version=1,
                committed_at=NOW + timedelta(seconds=4),
            )

            with self.assertRaisesRegex(RiskPolicyAuthorityError, "roll back"):
                registry.activate(
                    scope=exact_scope,
                    policy_id="core",
                    version=1,
                    committed_at=NOW + timedelta(seconds=5),
                )

            resolved = registry.resolve_current(exact_scope)
            self.assertEqual(resolved.identity.policy_id, "emergency")
            self.assertEqual(resolved.identity.version, 1)

    def test_hostile_durable_replay_cannot_reactivate_older_lineage_version(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            exact_scope = _scope()
            core_v1 = _policy("2.0")
            core_v2 = _policy("1.5")
            emergency_v1 = _policy("1.0")

            aggregate_version = 0

            def append_registration(policy_id, version, policy, when):
                nonlocal aggregate_version
                aggregate_version += 1
                identity = RiskPolicyIdentity(
                    policy_id=policy_id,
                    version=version,
                    content_digest=risk_policy_digest(policy),
                    scope=exact_scope,
                )
                payload = {
                    "schema_version": authority._SCHEMA_VERSION,
                    "operation": "REGISTER",
                    "identity": identity.payload(),
                    "policy": risk_policy_payload(policy),
                }
                store.append_event(
                    {
                        "event_id": authority._event_id(
                            "risk-policy-register",
                            payload,
                        ),
                        "event_type": authority._REGISTER_EVENT,
                        "aggregate_type": authority._AGGREGATE_TYPE,
                        "aggregate_id": exact_scope.aggregate_id,
                        "aggregate_version": str(aggregate_version),
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                        "committed_at": when.isoformat().replace("+00:00", "Z"),
                    }
                )
                return identity

            def append_activation(identity, when):
                nonlocal aggregate_version
                aggregate_version += 1
                payload = {
                    "schema_version": authority._SCHEMA_VERSION,
                    "operation": "ACTIVATE",
                    "identity": identity.payload(),
                }
                store.append_event(
                    {
                        "event_id": authority._event_id(
                            "risk-policy-activate",
                            payload,
                        ),
                        "event_type": authority._ACTIVATE_EVENT,
                        "aggregate_type": authority._AGGREGATE_TYPE,
                        "aggregate_id": exact_scope.aggregate_id,
                        "aggregate_version": str(aggregate_version),
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                        "committed_at": when.isoformat().replace("+00:00", "Z"),
                    }
                )

            identity_v1 = append_registration("core", 1, core_v1, NOW)
            identity_v2 = append_registration(
                "core",
                2,
                core_v2,
                NOW + timedelta(seconds=1),
            )
            emergency = append_registration(
                "emergency",
                1,
                emergency_v1,
                NOW + timedelta(seconds=2),
            )
            append_activation(identity_v2, NOW + timedelta(seconds=3))
            append_activation(emergency, NOW + timedelta(seconds=4))
            append_activation(identity_v1, NOW + timedelta(seconds=5))

            with self.assertRaisesRegex(RiskPolicyAuthorityError, "roll back"):
                DurableRiskPolicyRegistry(JournalStore(path)).resolve_current(exact_scope)

    def test_stale_activation_cannot_append_after_newer_same_scope_activation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            concurrent_store = JournalStore(path)
            registry = DurableRiskPolicyRegistry(store)
            concurrent = DurableRiskPolicyRegistry(concurrent_store)
            exact_scope = _scope()

            registry.register(
                scope=exact_scope,
                policy_id="core",
                version=1,
                policy=_policy("2.0"),
                committed_at=NOW,
            )
            registry.register(
                scope=exact_scope,
                policy_id="core",
                version=2,
                policy=_policy("1.5"),
                committed_at=NOW + timedelta(seconds=1),
            )
            original_append = store.append_event
            triggered = False

            def append_after_newer_activation(envelope, *args, **kwargs):
                nonlocal triggered
                if not triggered:
                    triggered = True
                    concurrent.activate(
                        scope=exact_scope,
                        policy_id="core",
                        version=2,
                        committed_at=NOW + timedelta(seconds=2),
                    )
                return original_append(envelope, *args, **kwargs)

            with patch.object(
                store,
                "append_event",
                side_effect=append_after_newer_activation,
            ):
                with self.assertRaisesRegex(
                    RiskPolicyAuthorityError,
                    "changed concurrently",
                ):
                    registry.activate(
                        scope=exact_scope,
                        policy_id="core",
                        version=1,
                        committed_at=NOW + timedelta(seconds=3),
                    )

            restarted = DurableRiskPolicyRegistry(JournalStore(path))
            self.assertEqual(restarted.resolve_current(exact_scope).identity.version, 2)
            events = restarted.store.load_events(
                authority._AGGREGATE_TYPE,
                exact_scope.aggregate_id,
            )
            self.assertEqual([event["aggregate_version"] for event in events], [1, 2, 3])

    def test_durable_policy_identity_covers_every_risk_policy_field(self):
        policy = _policy("2.0")
        serialized_fields = set(risk_policy_payload(policy)) - {"schema_version"}
        runtime_fields = {field.name for field in fields(RiskPolicy)}
        self.assertEqual(serialized_fields, runtime_fields)


if __name__ == "__main__":
    unittest.main()
