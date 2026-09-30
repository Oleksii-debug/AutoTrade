from datetime import datetime, timedelta, timezone, tzinfo
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


    def test_explicit_activation_episodes_detour_restart_and_historical_cuts(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            registry.register(
                scope=exact_scope, policy_id="core", version=1,
                policy=policy(), committed_at=NOW,
            )
            registry.register(
                scope=exact_scope, policy_id="emergency", version=1,
                policy=policy(max_gross_leverage="1"),
                committed_at=NOW + timedelta(seconds=1),
            )
            self.assertTrue(registry.activate(
                scope=exact_scope, policy_id="core", version=1,
                committed_at=NOW + timedelta(seconds=2),
                activation_request_id="core-episode-1",
                expected_previous_activation_event_id=None,
            ))
            first = registry.resolve_current(exact_scope)
            cut_first = store.current_journal_sequence()
            self.assertTrue(registry.activate(
                scope=exact_scope, policy_id="emergency", version=1,
                committed_at=NOW + timedelta(seconds=3),
                activation_request_id="emergency-episode-1",
                expected_previous_activation_event_id=first.activation_event_id,
            ))
            second = registry.resolve_current(exact_scope)
            cut_second = store.current_journal_sequence()
            self.assertTrue(registry.activate(
                scope=exact_scope, policy_id="core", version=1,
                committed_at=NOW + timedelta(seconds=4),
                activation_request_id="core-episode-2",
                expected_previous_activation_event_id=second.activation_event_id,
            ))
            third = registry.resolve_current(exact_scope)
            self.assertEqual(third.identity.policy_id, "core")
            self.assertEqual(len({
                first.activation_event_id, second.activation_event_id,
                third.activation_event_id,
            }), 3)
            self.assertEqual(
                [e["event_type"] for e in store.load_events(
                    authority._AGGREGATE_TYPE, exact_scope.aggregate_id
                ) if e["event_type"].startswith("RiskPolicyActivated")],
                [authority._ACTIVATE_EVENT_V2] * 3,
            )
            restarted = DurableRiskPolicyRegistry(JournalStore(path))
            self.assertEqual(restarted.resolve_current(exact_scope), third)
            self.assertEqual(
                restarted.resolve_current(exact_scope, journal_sequence_cut=cut_first),
                first,
            )
            self.assertEqual(
                restarted.resolve_current(exact_scope, journal_sequence_cut=cut_second),
                second,
            )

    def test_superseded_exact_retry_conflict_and_predecessor_stale_are_read_only(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            for name in ("core", "emergency"):
                registry.register(
                    scope=exact_scope, policy_id=name, version=1,
                    policy=policy(), committed_at=NOW,
                )
            kwargs = dict(
                scope=exact_scope, policy_id="core", version=1,
                committed_at=NOW + timedelta(seconds=1),
                activation_request_id="first-intent",
                expected_previous_activation_event_id=None,
            )
            self.assertTrue(registry.activate(**kwargs))
            current = registry.resolve_current(exact_scope)
            self.assertFalse(registry.activate(
                **{**kwargs, "committed_at": NOW + timedelta(seconds=2)}
            ))
            self.assertTrue(registry.activate(
                scope=exact_scope, policy_id="emergency", version=1,
                committed_at=NOW + timedelta(seconds=3),
                activation_request_id="second-intent",
                expected_previous_activation_event_id=current.activation_event_id,
            ))
            cut = store.current_journal_sequence()
            with self.assertRaisesRegex(RiskPolicyAuthorityError, "superseded"):
                registry.activate(**kwargs)
            with self.assertRaisesRegex(RiskPolicyAuthorityError, "conflicts"):
                registry.activate(
                    **{**kwargs, "policy_id": "emergency",
                       "expected_previous_activation_event_id":
                           current.activation_event_id}
                )
            with self.assertRaisesRegex(RiskPolicyAuthorityError, "predecessor"):
                registry.activate(
                    **{**kwargs, "activation_request_id": "third-intent"}
                )
            self.assertEqual(store.current_journal_sequence(), cut)
            self.assertEqual(
                registry.resolve_current(exact_scope).identity.policy_id, "emergency"
            )

    def test_legacy_reselection_after_detour_requires_new_explicit_intent(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            for name in ("core", "emergency"):
                registry.register(
                    scope=exact_scope, policy_id=name, version=1,
                    policy=policy(), committed_at=NOW,
                )
            self.assertTrue(registry.activate(
                scope=exact_scope, policy_id="core", version=1, committed_at=NOW,
            ))
            first = registry.resolve_current(exact_scope)
            self.assertFalse(registry.activate(
                scope=exact_scope, policy_id="core", version=1,
                committed_at=NOW + timedelta(seconds=1),
            ))
            self.assertTrue(registry.activate(
                scope=exact_scope, policy_id="emergency", version=1,
                committed_at=NOW + timedelta(seconds=2),
            ))
            second = registry.resolve_current(exact_scope)
            cut = store.current_journal_sequence()
            with self.assertRaisesRegex(RiskPolicyAuthorityError, "legacy activation"):
                registry.activate(
                    scope=exact_scope, policy_id="core", version=1,
                    committed_at=NOW + timedelta(seconds=3),
                )
            self.assertEqual(store.current_journal_sequence(), cut)
            self.assertEqual(
                registry.resolve_current(
                    exact_scope, journal_sequence_cut=first.activation_journal_sequence
                ), first,
            )
            self.assertTrue(registry.activate(
                scope=exact_scope, policy_id="core", version=1,
                committed_at=NOW + timedelta(seconds=4),
                activation_request_id="explicit-return-core",
                expected_previous_activation_event_id=second.activation_event_id,
            ))

    def test_same_scope_rival_activation_wins_cas_not_same_key_shortcut(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            rival_store = JournalStore(path)
            registry = DurableRiskPolicyRegistry(store)
            rival = DurableRiskPolicyRegistry(rival_store)
            exact_scope = scope()
            for name in ("core", "emergency"):
                registry.register(
                    scope=exact_scope, policy_id=name, version=1,
                    policy=policy(), committed_at=NOW,
                )
            registry.activate(
                scope=exact_scope, policy_id="core", version=1,
                committed_at=NOW + timedelta(seconds=1),
                activation_request_id="first", expected_previous_activation_event_id=None,
            )
            predecessor = registry.resolve_current(exact_scope).activation_event_id
            original_append = store.append_event
            triggered = False

            def interleaved_append(envelope, *args, **kwargs):
                nonlocal triggered
                if not triggered:
                    triggered = True
                    rival.activate(
                        scope=exact_scope, policy_id="emergency", version=1,
                        committed_at=NOW + timedelta(seconds=2),
                        activation_request_id="rival",
                        expected_previous_activation_event_id=predecessor,
                    )
                return original_append(envelope, *args, **kwargs)

            with patch.object(store, "append_event", side_effect=interleaved_append):
                with self.assertRaisesRegex(
                    RiskPolicyAuthorityError, "changed concurrently"
                ):
                    registry.activate(
                        scope=exact_scope, policy_id="emergency", version=1,
                        committed_at=NOW + timedelta(seconds=3),
                        activation_request_id="stale-racer",
                        expected_previous_activation_event_id=predecessor,
                    )
            self.assertEqual(
                DurableRiskPolicyRegistry(JournalStore(path))
                .resolve_current(exact_scope).identity.policy_id,
                "emergency",
            )
            self.assertEqual(
                len([e for e in store.load_events(
                    authority._AGGREGATE_TYPE, exact_scope.aggregate_id
                ) if e["event_type"] == authority._ACTIVATE_EVENT_V2]),
                2,
            )

    def test_direct_durable_v2_wrong_predecessor_fails_closed_on_replay(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            registry.register(
                scope=exact_scope, policy_id="core", version=1,
                policy=policy(), committed_at=NOW,
            )
            identity = registry._current_state(exact_scope)[1].registered[
                ("core", 1)
            ][0]
            forged = {
                "schema_version": authority._ACTIVATE_V2_SCHEMA_VERSION,
                "operation": "ACTIVATE",
                "identity": identity.payload(),
                "activation_request_id": "forged-predecessor",
                "expected_previous_activation_event_id":
                    "risk-policy-activate:" + "0" * 64,
            }
            store.append_event({
                "event_id": authority._event_id("risk-policy-activate-v2", forged),
                "event_type": authority._ACTIVATE_EVENT_V2,
                "aggregate_type": authority._AGGREGATE_TYPE,
                "aggregate_id": exact_scope.aggregate_id,
                "aggregate_version": "2",
                "payload": forged, "payload_hash": payload_digest(forged),
                "committed_at": NOW.isoformat().replace("+00:00", "Z"),
            })
            with self.assertRaisesRegex(RiskPolicyAuthorityError, "predecessor mismatch"):
                DurableRiskPolicyRegistry(store).resolve_current(exact_scope)

    def test_explicit_noop_cannot_be_reused_after_predecessor_detour(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            for name in ("core", "emergency"):
                registry.register(
                    scope=exact_scope, policy_id=name, version=1,
                    policy=policy(), committed_at=NOW,
                )
            registry.activate(
                scope=exact_scope, policy_id="core", version=1, committed_at=NOW,
                activation_request_id="core-first", expected_previous_activation_event_id=None,
            )
            current = registry.resolve_current(exact_scope)
            noop = dict(
                scope=exact_scope, policy_id="core", version=1,
                committed_at=NOW + timedelta(seconds=1),
                activation_request_id="noop-not-persisted",
                expected_previous_activation_event_id=current.activation_event_id,
            )
            unchanged = store.current_journal_sequence()
            with self.assertRaisesRegex(
                RiskPolicyAuthorityError, "already-active policy"
            ):
                registry.activate(**noop)
            self.assertEqual(store.current_journal_sequence(), unchanged)
            self.assertNotIn(
                "noop-not-persisted",
                registry._current_state(exact_scope)[1].activation_requests,
            )
            registry.activate(
                scope=exact_scope, policy_id="emergency", version=1,
                committed_at=NOW + timedelta(seconds=2),
                activation_request_id="emergency",
                expected_previous_activation_event_id=current.activation_event_id,
            )
            cut = store.current_journal_sequence()
            with self.assertRaisesRegex(RiskPolicyAuthorityError, "predecessor"):
                registry.activate(**noop)
            self.assertEqual(store.current_journal_sequence(), cut)



    def test_exact_lost_response_after_real_append_does_not_reactivate_superseded(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            for name in ("core", "emergency"):
                registry.register(
                    scope=exact_scope, policy_id=name, version=1,
                    policy=policy(), committed_at=NOW,
                )
            before = store.current_journal_sequence()
            actual_append = store.append_event

            def committed_but_reply_lost(envelope, *args, **kwargs):
                result = actual_append(envelope, *args, **kwargs)
                self.assertTrue(result.inserted)
                raise ValueError("synthetic lost reply after durable append")

            with patch.object(
                store, "append_event", side_effect=committed_but_reply_lost,
            ):
                self.assertFalse(registry.activate(
                    scope=exact_scope, policy_id="core", version=1,
                    committed_at=NOW + timedelta(seconds=1),
                    activation_request_id="lost-response-core",
                    expected_previous_activation_event_id=None,
                ))
            self.assertEqual(store.current_journal_sequence(), before + 1)
            first = registry.resolve_current(exact_scope)
            fresh_reader = DurableRiskPolicyRegistry(JournalStore(path))
            self.assertFalse(fresh_reader.activate(
                scope=exact_scope, policy_id="core", version=1,
                committed_at=NOW + timedelta(seconds=2),
                activation_request_id="lost-response-core",
                expected_previous_activation_event_id=None,
            ))
            self.assertEqual(store.current_journal_sequence(), before + 1)
            registry.activate(
                scope=exact_scope, policy_id="emergency", version=1,
                committed_at=NOW + timedelta(seconds=3),
                activation_request_id="emergency-after-lost",
                expected_previous_activation_event_id=first.activation_event_id,
            )
            after_detour = store.current_journal_sequence()
            with self.assertRaisesRegex(
                RiskPolicyAuthorityError, "superseded episode",
            ):
                fresh_reader.activate(
                    scope=exact_scope, policy_id="core", version=1,
                    committed_at=NOW + timedelta(seconds=4),
                    activation_request_id="lost-response-core",
                    expected_previous_activation_event_id=None,
                )
            self.assertEqual(store.current_journal_sequence(), after_detour)
            self.assertEqual(
                fresh_reader.resolve_current(exact_scope).identity.policy_id,
                "emergency",
            )

    def test_committed_at_rejects_polymorphic_datetime_before_callbacks(self):
        class HostileDateTime(datetime):
            callbacks = 0

            def utcoffset(self):
                type(self).callbacks += 1
                raise AssertionError("datetime subclass callback executed")

            def astimezone(self, *args, **kwargs):
                type(self).callbacks += 1
                raise AssertionError("datetime subclass callback executed")

        class HostileTimezone(tzinfo):
            def __init__(self):
                self.callbacks = 0

            def utcoffset(self, value):
                self.callbacks += 1
                raise AssertionError("tzinfo callback executed")

            def dst(self, value):
                self.callbacks += 1
                raise AssertionError("tzinfo callback executed")

            def tzname(self, value):
                self.callbacks += 1
                raise AssertionError("tzinfo callback executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            registry.register(
                scope=exact_scope, policy_id="core", version=1,
                policy=policy(), committed_at=NOW,
            )
            hostile_zone = HostileTimezone()
            malformed = (
                HostileDateTime(
                    2026, 9, 30, 2, 30, tzinfo=timezone.utc
                ),
                datetime(
                    2026, 9, 30, 2, 30, tzinfo=hostile_zone
                ),
            )
            unchanged = store.current_journal_sequence()
            for bad in malformed:
                with self.subTest(kind=type(bad).__name__):
                    with self.assertRaisesRegex(
                        RiskPolicyAuthorityError, "exact datetime"
                    ):
                        registry.register(
                            scope=exact_scope, policy_id="other", version=1,
                            policy=policy(), committed_at=bad,
                        )
                    with self.assertRaisesRegex(
                        RiskPolicyAuthorityError, "exact datetime"
                    ):
                        registry.activate(
                            scope=exact_scope, policy_id="core", version=1,
                            committed_at=bad,
                        )
                    self.assertEqual(store.current_journal_sequence(), unchanged)
            self.assertEqual(HostileDateTime.callbacks, 0)
            self.assertEqual(hostile_zone.callbacks, 0)
            self.assertEqual(
                authority._utc_text(
                    datetime(
                        2026, 9, 30, 2, 30,
                        tzinfo=timezone(timedelta(hours=5, minutes=30)),
                    ),
                    name="committed_at",
                ),
                "2026-09-29T21:00:00Z",
            )

    def test_request_outcomes_preserve_exact_global_journal_sequence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            other_scope = scope(provider_environment="DEMO")
            for name in ("core", "emergency"):
                registry.register(
                    scope=exact_scope, policy_id=name, version=1,
                    policy=policy(), committed_at=NOW,
                )
            registry.register(
                scope=other_scope, policy_id="core", version=1,
                policy=policy(), committed_at=NOW,
            )
            self.assertTrue(registry.activate(
                scope=exact_scope, policy_id="core", version=1,
                committed_at=NOW + timedelta(seconds=1),
                activation_request_id="first-sequence",
                expected_previous_activation_event_id=None,
            ))
            first = registry.resolve_current(exact_scope)
            # A different scope moves the global journal without changing this
            # scope's exact durable request outcome or predecessor.
            self.assertTrue(registry.activate(
                scope=other_scope, policy_id="core", version=1,
                committed_at=NOW + timedelta(seconds=2),
                activation_request_id="other-scope-sequence",
                expected_previous_activation_event_id=None,
            ))
            self.assertTrue(registry.activate(
                scope=exact_scope, policy_id="emergency", version=1,
                committed_at=NOW + timedelta(seconds=3),
                activation_request_id="second-sequence",
                expected_previous_activation_event_id=first.activation_event_id,
            ))
            second = registry.resolve_current(exact_scope)
            current = registry._current_state(exact_scope)[1]
            first_outcome = current.activation_requests["first-sequence"]
            second_outcome = current.activation_requests["second-sequence"]
            self.assertEqual(
                first_outcome,
                (first.identity, None, first.activation_event_id,
                 first.activation_journal_sequence),
            )
            self.assertEqual(
                second_outcome,
                (second.identity, first.activation_event_id,
                 second.activation_event_id, second.activation_journal_sequence),
            )
            self.assertGreater(
                second.activation_journal_sequence,
                first.activation_journal_sequence + 1,
            )
            historical = registry._replay(
                exact_scope,
                journal_sequence_cut=first.activation_journal_sequence,
            )
            self.assertEqual(
                historical.activation_requests["first-sequence"], first_outcome,
            )
            self.assertNotIn("second-sequence", historical.activation_requests)
            restarted = DurableRiskPolicyRegistry(JournalStore(path))
            self.assertEqual(
                restarted._current_state(exact_scope)[1].activation_requests,
                current.activation_requests,
            )
            before_retry = store.current_journal_sequence()
            with self.assertRaisesRegex(
                RiskPolicyAuthorityError, "superseded episode",
            ):
                restarted.activate(
                    scope=exact_scope, policy_id="core", version=1,
                    committed_at=NOW + timedelta(seconds=4),
                    activation_request_id="first-sequence",
                    expected_previous_activation_event_id=None,
                )
            self.assertEqual(store.current_journal_sequence(), before_retry)

    def test_durable_duplicate_v2_request_id_is_rejected_even_with_new_event_id(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            for name in ("core", "emergency"):
                registry.register(
                    scope=exact_scope, policy_id=name, version=1,
                    policy=policy(), committed_at=NOW,
                )
            registry.activate(
                scope=exact_scope, policy_id="core", version=1,
                committed_at=NOW + timedelta(seconds=1),
                activation_request_id="shared-intent",
                expected_previous_activation_event_id=None,
            )
            previous = registry.resolve_current(exact_scope).activation_event_id
            emergency_identity = registry._current_state(exact_scope)[1].registered[
                ("emergency", 1)
            ][0]
            tampered = {
                "schema_version": authority._ACTIVATE_V2_SCHEMA_VERSION,
                "operation": "ACTIVATE",
                "identity": emergency_identity.payload(),
                "activation_request_id": "shared-intent",
                "expected_previous_activation_event_id": previous,
            }
            store.append_event({
                "event_id": authority._event_id("risk-policy-activate-v2", tampered),
                "event_type": authority._ACTIVATE_EVENT_V2,
                "aggregate_type": authority._AGGREGATE_TYPE,
                "aggregate_id": exact_scope.aggregate_id,
                "aggregate_version": str(store.next_aggregate_version(
                    authority._AGGREGATE_TYPE, exact_scope.aggregate_id
                )),
                "payload": tampered, "payload_hash": payload_digest(tampered),
                "committed_at": (NOW + timedelta(seconds=2))
                    .isoformat().replace("+00:00", "Z"),
            })
            with self.assertRaisesRegex(
                RiskPolicyAuthorityError, "duplicate durable activation_request_id"
            ):
                registry.resolve_current(exact_scope)

    def test_unrelated_scope_interleaving_does_not_conflict_explicit_activation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            other_store = JournalStore(path)
            registry = DurableRiskPolicyRegistry(store)
            other = DurableRiskPolicyRegistry(other_store)
            exact_scope = scope()
            demo = scope(provider_environment="DEMO")
            for item in (exact_scope, demo):
                registry.register(
                    scope=item, policy_id="core", version=1,
                    policy=policy(), committed_at=NOW,
                )
            actual_append = store.append_event
            triggered = False

            def after_unrelated_activation(envelope, *args, **kwargs):
                nonlocal triggered
                if not triggered:
                    triggered = True
                    self.assertTrue(other.activate(
                        scope=demo, policy_id="core", version=1,
                        committed_at=NOW + timedelta(seconds=1),
                        activation_request_id="demo-intent",
                        expected_previous_activation_event_id=None,
                    ))
                return actual_append(envelope, *args, **kwargs)

            with patch.object(
                store, "append_event", side_effect=after_unrelated_activation
            ):
                self.assertTrue(registry.activate(
                    scope=exact_scope, policy_id="core", version=1,
                    committed_at=NOW + timedelta(seconds=2),
                    activation_request_id="testnet-intent",
                    expected_previous_activation_event_id=None,
                ))
            self.assertNotEqual(
                registry.resolve_current(exact_scope).activation_event_id,
                other.resolve_current(demo).activation_event_id,
            )

    def test_activation_intent_is_exact_bounded_ascii_before_financial_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = scope()
            registry.register(
                scope=exact_scope, policy_id="core", version=1,
                policy=policy(), committed_at=NOW,
            )
            unchanged = store.current_journal_sequence()
            for malformed in ("", " leading", "trailing ", "é", "a" * 129, True):
                with self.subTest(malformed=malformed):
                    with self.assertRaisesRegex(
                        RiskPolicyAuthorityError, "canonical non-empty ASCII"
                    ):
                        registry.activate(
                            scope=exact_scope, policy_id="core", version=1,
                            committed_at=NOW,
                            activation_request_id=malformed,
                            expected_previous_activation_event_id=None,
                        )
                    self.assertEqual(store.current_journal_sequence(), unchanged)


if __name__ == "__main__":
    unittest.main()
