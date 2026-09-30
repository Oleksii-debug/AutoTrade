from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.risk import RiskPolicy
from mvp.autotrade_mvp import risk_policy_authority as authority
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyAuthorityError,
    RiskPolicyIdentity,
    RiskPolicyScope,
    risk_policy_digest,
    risk_policy_payload,
)


NOW = datetime(2026, 9, 30, 2, 30, tzinfo=timezone.utc)


def scope(*, provider_environment="TESTNET", entity_policy_id="bybit-global-v1"):
    return RiskPolicyScope(
        provider_id="BYBIT",
        account_id="account-1",
        environment="PAPER",
        provider_environment=provider_environment,
        entity_policy_id=entity_policy_id,
        instrument_family="PERPETUAL",
    )


def policy(*, max_gross_leverage="2", max_daily_loss="100"):
    return RiskPolicy.create(
        max_abs_position="100",
        max_single_notional="1000",
        max_gross_leverage=max_gross_leverage,
        max_net_leverage="1.5",
        max_daily_loss=max_daily_loss,
        max_drawdown_fraction="0.20",
        max_data_age_seconds="5",
        max_fx_age_seconds="5",
        min_margin_headroom="0.10",
        max_stress_loss="200",
        max_asset_concentration_fraction="0.75",
        max_venue_concentration_fraction="0.80",
        max_order_participation_fraction="0.10",
        max_spread_fraction="0.01",
        max_slippage_fraction="0.02",
        max_clock_age_seconds="2",
        allowed_actions=("TRADE", "REDUCE", "HEDGE", "FLATTEN"),
        require_settlement_evidence=True,
    )


class DurableRiskPolicyRegistryTests(unittest.TestCase):
    def test_registration_is_content_addressed_idempotent_and_conflicts_on_reuse(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            first = policy()

            self.assertTrue(
                registry.register(
                    scope=exact_scope,
                    policy_id="core-risk",
                    version=1,
                    policy=first,
                    committed_at=NOW,
                )
            )
            sequence = store.current_journal_sequence()
            self.assertFalse(
                registry.register(
                    scope=exact_scope,
                    policy_id="core-risk",
                    version=1,
                    policy=first,
                    committed_at=NOW + timedelta(seconds=10),
                )
            )
            self.assertEqual(store.current_journal_sequence(), sequence)

            with self.assertRaisesRegex(
                RiskPolicyAuthorityError,
                "different content",
            ):
                registry.register(
                    scope=exact_scope,
                    policy_id="core-risk",
                    version=1,
                    policy=policy(max_gross_leverage="3"),
                    committed_at=NOW + timedelta(seconds=20),
                )

    def test_testnet_and_demo_are_distinct_policy_authority_scopes(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            registry = DurableRiskPolicyRegistry(store)
            testnet = scope(provider_environment="TESTNET")
            demo = scope(provider_environment="DEMO")
            same_policy = policy()

            self.assertNotEqual(testnet, demo)
            self.assertNotEqual(testnet.aggregate_id, demo.aggregate_id)
            self.assertTrue(
                registry.register(
                    scope=testnet,
                    policy_id="core-risk",
                    version=1,
                    policy=same_policy,
                    committed_at=NOW,
                )
            )
            self.assertTrue(
                registry.activate(
                    scope=testnet,
                    policy_id="core-risk",
                    version=1,
                    committed_at=NOW + timedelta(seconds=1),
                )
            )
            with self.assertRaisesRegex(
                RiskPolicyAuthorityError,
                "no unambiguous active",
            ):
                registry.resolve_current(demo)

            self.assertTrue(
                registry.register(
                    scope=demo,
                    policy_id="core-risk",
                    version=1,
                    policy=same_policy,
                    committed_at=NOW + timedelta(seconds=2),
                )
            )
            self.assertTrue(
                registry.activate(
                    scope=demo,
                    policy_id="core-risk",
                    version=1,
                    committed_at=NOW + timedelta(seconds=3),
                )
            )
            testnet_resolved = registry.resolve_current(testnet)
            demo_resolved = registry.resolve_current(demo)
            self.assertEqual(
                testnet_resolved.identity.content_digest,
                demo_resolved.identity.content_digest,
            )
            self.assertNotEqual(testnet_resolved.identity, demo_resolved.identity)

    def test_activation_advances_new_commands_but_exact_old_cut_replays_v1(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            v1 = policy(max_gross_leverage="2")
            v2 = policy(max_gross_leverage="1.25")

            registry.register(
                scope=exact_scope,
                policy_id="core-risk",
                version=1,
                policy=v1,
                committed_at=NOW,
            )
            registry.activate(
                scope=exact_scope,
                policy_id="core-risk",
                version=1,
                committed_at=NOW + timedelta(seconds=1),
            )
            v1_cut = store.current_journal_sequence()
            resolved_v1 = registry.resolve_current(
                exact_scope,
                journal_sequence_cut=v1_cut,
            )

            registry.register(
                scope=exact_scope,
                policy_id="core-risk",
                version=2,
                policy=v2,
                committed_at=NOW + timedelta(seconds=2),
            )
            registry.activate(
                scope=exact_scope,
                policy_id="core-risk",
                version=2,
                committed_at=NOW + timedelta(seconds=3),
            )

            self.assertEqual(resolved_v1.identity.version, 1)
            self.assertEqual(resolved_v1.policy, v1)
            current = registry.resolve_current(exact_scope)
            self.assertEqual(current.identity.version, 2)
            self.assertEqual(current.policy, v2)
            self.assertGreater(
                current.activation_journal_sequence,
                resolved_v1.activation_journal_sequence,
            )

            restarted = DurableRiskPolicyRegistry(JournalStore(path))
            self.assertEqual(restarted.resolve_current(exact_scope), current)
            self.assertEqual(
                restarted.resolve_current(
                    exact_scope,
                    journal_sequence_cut=v1_cut,
                ),
                resolved_v1,
            )

    def test_policy_lineage_cannot_roll_back_after_newer_activation(self):
        with TemporaryDirectory() as directory:
            registry = DurableRiskPolicyRegistry(
                JournalStore(Path(directory) / "journal.sqlite3")
            )
            exact_scope = scope()
            registry.register(
                scope=exact_scope,
                policy_id="core-risk",
                version=1,
                policy=policy(max_gross_leverage="2"),
                committed_at=NOW,
            )
            registry.activate(
                scope=exact_scope,
                policy_id="core-risk",
                version=1,
                committed_at=NOW + timedelta(seconds=1),
            )
            registry.register(
                scope=exact_scope,
                policy_id="core-risk",
                version=2,
                policy=policy(max_gross_leverage="1.5"),
                committed_at=NOW + timedelta(seconds=2),
            )
            registry.activate(
                scope=exact_scope,
                policy_id="core-risk",
                version=2,
                committed_at=NOW + timedelta(seconds=3),
            )
            with self.assertRaisesRegex(RiskPolicyAuthorityError, "roll back"):
                registry.activate(
                    scope=exact_scope,
                    policy_id="core-risk",
                    version=1,
                    committed_at=NOW + timedelta(seconds=4),
                )

    def test_activation_requires_exact_registered_scope(self):
        with TemporaryDirectory() as directory:
            registry = DurableRiskPolicyRegistry(
                JournalStore(Path(directory) / "journal.sqlite3")
            )
            with self.assertRaisesRegex(
                RiskPolicyAuthorityError,
                "registered before activation",
            ):
                registry.activate(
                    scope=scope(provider_environment="DEMO"),
                    policy_id="core-risk",
                    version=1,
                    committed_at=NOW,
                )

    def test_semantically_tampered_registration_digest_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            exact_scope = scope()
            registered_policy = policy()
            forged_identity = RiskPolicyIdentity(
                policy_id="core-risk",
                version=1,
                content_digest="sha256:" + "0" * 64,
                scope=exact_scope,
            )
            payload = {
                "schema_version": authority._SCHEMA_VERSION,
                "operation": "REGISTER",
                "identity": forged_identity.payload(),
                "policy": risk_policy_payload(registered_policy),
            }
            store.append_event(
                {
                    "event_id": authority._event_id("risk-policy-register", payload),
                    "event_type": authority._REGISTER_EVENT,
                    "aggregate_type": authority._AGGREGATE_TYPE,
                    "aggregate_id": exact_scope.aggregate_id,
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": NOW.isoformat().replace("+00:00", "Z"),
                }
            )

            with self.assertRaisesRegex(
                RiskPolicyAuthorityError,
                "content digest mismatch",
            ):
                DurableRiskPolicyRegistry(store).resolve_current(exact_scope)

    def test_durable_replay_rejects_noncanonical_decimal_before_risk_policy_create(self):
        malformed_values = (
            "1e2",
            "1.0",
            "+1",
            "-0",
            "1\n",
            "1" + "0" * 256,
        )
        for malformed in malformed_values:
            with self.subTest(malformed=malformed), TemporaryDirectory() as directory:
                store = JournalStore(Path(directory) / "journal.sqlite3")
                exact_scope = scope()
                durable_policy = risk_policy_payload(policy())
                durable_policy["max_gross_leverage"] = malformed
                identity = RiskPolicyIdentity(
                    policy_id="core-risk",
                    version=1,
                    content_digest="sha256:" + "0" * 64,
                    scope=exact_scope,
                )
                payload = {
                    "schema_version": authority._SCHEMA_VERSION,
                    "operation": "REGISTER",
                    "identity": identity.payload(),
                    "policy": durable_policy,
                }
                store.append_event(
                    {
                        "event_id": authority._event_id("risk-policy-register", payload),
                        "event_type": authority._REGISTER_EVENT,
                        "aggregate_type": authority._AGGREGATE_TYPE,
                        "aggregate_id": exact_scope.aggregate_id,
                        "aggregate_version": "1",
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                        "committed_at": NOW.isoformat().replace("+00:00", "Z"),
                    }
                )
                with patch.object(
                    authority.RiskPolicy,
                    "create",
                    side_effect=AssertionError("RiskPolicy.create must not run"),
                ) as create:
                    with self.assertRaisesRegex(
                        RiskPolicyAuthorityError,
                        "canonical bounded Decimal text",
                    ):
                        DurableRiskPolicyRegistry(store).resolve_current(exact_scope)
                    create.assert_not_called()

    def test_same_scope_registration_uses_replay_cut_as_aggregate_cas(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            concurrent_store = JournalStore(path)
            registry = DurableRiskPolicyRegistry(store)
            concurrent = DurableRiskPolicyRegistry(concurrent_store)
            exact_scope = scope()
            registry.register(
                scope=exact_scope,
                policy_id="core-risk",
                version=1,
                policy=policy(max_gross_leverage="2"),
                committed_at=NOW,
            )
            original_append = store.append_event
            triggered = False

            def append_after_concurrent_writer(envelope, *args, **kwargs):
                nonlocal triggered
                if not triggered:
                    triggered = True
                    concurrent.register(
                        scope=exact_scope,
                        policy_id="core-risk",
                        version=3,
                        policy=policy(max_gross_leverage="1.25"),
                        committed_at=NOW + timedelta(seconds=1),
                    )
                return original_append(envelope, *args, **kwargs)

            with patch.object(
                store,
                "append_event",
                side_effect=append_after_concurrent_writer,
            ):
                with self.assertRaisesRegex(
                    RiskPolicyAuthorityError,
                    "changed concurrently",
                ):
                    registry.register(
                        scope=exact_scope,
                        policy_id="core-risk",
                        version=2,
                        policy=policy(max_gross_leverage="1.5"),
                        committed_at=NOW + timedelta(seconds=2),
                    )

            events = store.load_events(authority._AGGREGATE_TYPE, exact_scope.aggregate_id)
            self.assertEqual([event["aggregate_version"] for event in events], [1, 2])
            self.assertEqual(events[-1]["payload"]["identity"]["version"], 3)

    def test_unrelated_scope_advance_does_not_false_conflict_registration(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            concurrent_store = JournalStore(path)
            registry = DurableRiskPolicyRegistry(store)
            concurrent = DurableRiskPolicyRegistry(concurrent_store)
            exact_scope = scope()
            other_scope = scope(provider_environment="DEMO")
            original_append = store.append_event
            triggered = False

            def append_after_unrelated_writer(envelope, *args, **kwargs):
                nonlocal triggered
                if not triggered:
                    triggered = True
                    concurrent.register(
                        scope=other_scope,
                        policy_id="core-risk",
                        version=1,
                        policy=policy(max_gross_leverage="1.25"),
                        committed_at=NOW,
                    )
                return original_append(envelope, *args, **kwargs)

            with patch.object(
                store,
                "append_event",
                side_effect=append_after_unrelated_writer,
            ):
                self.assertTrue(
                    registry.register(
                        scope=exact_scope,
                        policy_id="core-risk",
                        version=1,
                        policy=policy(max_gross_leverage="2"),
                        committed_at=NOW + timedelta(seconds=1),
                    )
                )

            self.assertEqual(
                len(store.load_events(authority._AGGREGATE_TYPE, exact_scope.aggregate_id)),
                1,
            )
            self.assertEqual(
                len(store.load_events(authority._AGGREGATE_TYPE, other_scope.aggregate_id)),
                1,
            )

    def test_policy_digest_is_decimal_context_invariant(self):
        registered_policy = policy(
            max_gross_leverage="1.234567890123456789",
            max_daily_loss="123456789.123456789",
        )
        digests = []
        payloads = []
        for precision, rounding in (
            (4, ROUND_FLOOR),
            (4, ROUND_CEILING),
            (80, ROUND_FLOOR),
        ):
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                payloads.append(risk_policy_payload(registered_policy))
                digests.append(risk_policy_digest(registered_policy))
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[0], payloads[2])
        self.assertEqual(digests[0], digests[1])
        self.assertEqual(digests[0], digests[2])
        self.assertEqual(
            payloads[0]["max_daily_loss"],
            "123456789.123456789",
        )

    def test_decimal_subclass_is_rejected_before_virtual_dispatch(self):
        calls = []

        class HostileDecimal(Decimal):
            def is_finite(self):
                calls.append("is_finite")
                raise AssertionError("hostile Decimal method dispatched")

            def __format__(self, spec):
                calls.append("format")
                raise AssertionError("hostile Decimal method dispatched")

        hostile = HostileDecimal("100")
        forged = RiskPolicy(
            max_abs_position=hostile,
            max_single_notional=Decimal("1000"),
            max_gross_leverage=Decimal("2"),
            max_net_leverage=Decimal("1.5"),
            max_daily_loss=Decimal("100"),
            max_drawdown_fraction=Decimal("0.2"),
            max_data_age_seconds=Decimal("5"),
            max_fx_age_seconds=Decimal("5"),
            min_margin_headroom=Decimal("0.1"),
            max_stress_loss=Decimal("200"),
        )
        with self.assertRaisesRegex(
            RiskPolicyAuthorityError,
            "bounded exact Decimal",
        ):
            risk_policy_digest(forged)
        self.assertEqual(calls, [])

    def test_journal_store_subclass_cannot_forge_durable_authority(self):
        calls = []

        class HostileJournalStore(JournalStore):
            def current_journal_sequence(self):
                calls.append("current_journal_sequence")
                return 999

            def load_events(self, aggregate_type, aggregate_id):
                calls.append("load_events")
                return []

            def next_aggregate_version(self, aggregate_type, aggregate_id):
                calls.append("next_aggregate_version")
                return 1

            def append_event(self, envelope):
                calls.append("append_event")
                raise AssertionError("hostile append dispatched")

        with TemporaryDirectory() as directory:
            hostile = HostileJournalStore(Path(directory) / "journal.sqlite3")
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                DurableRiskPolicyRegistry(hostile)
        self.assertEqual(calls, [])

    def test_plain_journal_store_remains_accepted_after_exact_type_fence(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            registered_policy = policy()
            self.assertTrue(
                registry.register(
                    scope=exact_scope,
                    policy_id="core-risk",
                    version=1,
                    policy=registered_policy,
                    committed_at=NOW,
                )
            )
            self.assertTrue(
                registry.activate(
                    scope=exact_scope,
                    policy_id="core-risk",
                    version=1,
                    committed_at=NOW + timedelta(seconds=1),
                )
            )
            self.assertEqual(
                registry.resolve_current(exact_scope).identity.content_digest,
                risk_policy_digest(registered_policy),
            )

    def test_evidence_payload_binds_registration_activation_scope_and_cut(self):
        with TemporaryDirectory() as directory:
            registry = DurableRiskPolicyRegistry(
                JournalStore(Path(directory) / "journal.sqlite3")
            )
            exact_scope = scope(entity_policy_id="bybit-eu-v2")
            registered_policy = policy()
            registry.register(
                scope=exact_scope,
                policy_id="core-risk",
                version=7,
                policy=registered_policy,
                committed_at=NOW,
            )
            registry.activate(
                scope=exact_scope,
                policy_id="core-risk",
                version=7,
                committed_at=NOW + timedelta(seconds=1),
            )
            resolved = registry.resolve_current(exact_scope)
            evidence = resolved.evidence_payload
            self.assertEqual(evidence["identity"]["scope"], exact_scope.payload())
            self.assertEqual(
                evidence["identity"]["content_digest"],
                risk_policy_digest(registered_policy),
            )
            self.assertEqual(
                evidence["resolved_journal_sequence_cut"],
                resolved.activation_journal_sequence,
            )
            self.assertLessEqual(
                evidence["registration_journal_sequence"],
                evidence["activation_journal_sequence"],
            )


if __name__ == "__main__":
    unittest.main()
