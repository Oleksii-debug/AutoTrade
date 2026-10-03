from datetime import datetime, timedelta, timezone
from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.store_identity import JournalStoreIdentity
from mvp.autotrade_mvp import valuation_authority as authority
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyScope,
)
from mvp.autotrade_mvp.risk import RiskPolicy
from mvp.autotrade_mvp.valuation_authority import (
    DurableValuationBook,
    ValuationConflict,
    ValuationError,
    ValuationObservation,
    diagnostic_fx_observation,
    diagnostic_mark_observation,
    evaluate_valuation_freshness,
    observation_set_digest,
)


NOW = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
Q = "sha256:" + "1" * 64
R = "sha256:" + "2" * 64


def mark(
    *,
    provider_environment="TESTNET",
    value="100",
    source_event_at=None,
    available_at=NOW,
    observed_at=NOW,
    supersedes=None,
):
    return diagnostic_mark_observation(
        provider_id="BYBIT",
        account_id="acct-1",
        environment="PAPER",
        provider_environment=provider_environment,
        route_policy_id="bybit:v5:market-ticker",
        capability_snapshot_id="cap-snapshot-1",
        provider_qualification_id="qual-1",
        adapter_build_id="adapter-build-1",
        source_revision="source-rev-1",
        source_event_at=(
            NOW - timedelta(seconds=1)
            if source_event_at is None
            else source_event_at
        ),
        available_at=available_at,
        observed_at=observed_at,
        freshness_rule_id="risk-policy:mark-age-v1",
        origin_binding_id="diagnostic-origin-1",
        query_digest=Q,
        response_sha256=R,
        instrument_version="BTCUSDT@1",
        mark=Decimal(value),
        supersedes_observation_id=supersedes,
    )


def append_raw_valuation(store, book, observation, *, aggregate_version):
    """Append a structurally canonical row while bypassing the writer state machine."""
    durable_payload = {
        "schema_version": "1.0.0",
        "store_identity": authority._store_identity_payload(book.store_identity),
        "store_identity_digest": book.store_identity_digest,
        "observation": observation.to_contract_dict(),
    }
    store.append_event(
        {
            "event_id": observation.observation_id,
            "event_type": "ValuationObservation.v1",
            "aggregate_type": "valuation_observation",
            "aggregate_id": observation.scope_id,
            "aggregate_version": str(aggregate_version),
            "payload": durable_payload,
            "payload_hash": payload_digest(durable_payload),
            "committed_at": observation.observed_at.isoformat().replace("+00:00", "Z"),
        }
    )


def risk_scope():
    return RiskPolicyScope(
        provider_id="BYBIT",
        account_id="acct-1",
        environment="PAPER",
        provider_environment="TESTNET",
        entity_policy_id="bybit:v5:market-ticker",
        instrument_family="PERPETUAL",
    )


def risk_policy(*, data_age="5", fx_age="7"):
    return RiskPolicy.create(
        max_abs_position="100",
        max_single_notional="1000",
        max_gross_leverage="2",
        max_net_leverage="1.5",
        max_daily_loss="100",
        max_drawdown_fraction="0.2",
        max_data_age_seconds=data_age,
        max_fx_age_seconds=fx_age,
        min_margin_headroom="0.1",
        max_stress_loss="200",
    )


def resolved_policy(store, *, data_age="5", fx_age="7"):
    registry = DurableRiskPolicyRegistry(store)
    exact_scope = risk_scope()
    registry.register(
        scope=exact_scope,
        policy_id="core-risk",
        version=1,
        policy=risk_policy(data_age=data_age, fx_age=fx_age),
        committed_at=NOW,
    )
    registry.activate(
        scope=exact_scope,
        policy_id="core-risk",
        version=1,
        committed_at=NOW + timedelta(milliseconds=1),
    )
    return registry.resolve_current(exact_scope)


class DurableValuationAuthorityTests(unittest.TestCase):
    def test_store_generation_digest_uses_windows_handle_identity_not_path(self):
        first = JournalStoreIdentity(
            canonical_path="C:/AutoTrade/journal.sqlite3",
            filesystem_device=None,
            filesystem_inode=None,
            identity_source="windows_by_handle",
            windows_volume_serial=23,
            windows_file_index_high=7,
            windows_file_index_low=11,
        )
        alias = JournalStoreIdentity(
            canonical_path="c:/AUTOTRADE/JOURNAL.SQLITE3",
            filesystem_device=None,
            filesystem_inode=None,
            identity_source="windows_by_handle",
            windows_volume_serial=23,
            windows_file_index_high=7,
            windows_file_index_low=11,
        )
        other = JournalStoreIdentity(
            canonical_path="C:/AutoTrade/journal.sqlite3",
            filesystem_device=None,
            filesystem_inode=None,
            identity_source="windows_by_handle",
            windows_volume_serial=23,
            windows_file_index_high=7,
            windows_file_index_low=12,
        )

        self.assertEqual(
            authority._store_identity_payload(first),
            authority._store_identity_payload(alias),
        )
        self.assertEqual(
            authority._store_identity_digest(first),
            authority._store_identity_digest(alias),
        )
        self.assertNotEqual(
            authority._store_identity_digest(first),
            authority._store_identity_digest(other),
        )

    def test_provider_origin_cannot_be_publicly_self_asserted(self):
        with self.assertRaisesRegex(
            ValuationError,
            "product-owned issuer",
        ):
            ValuationObservation(
                kind="MARK",
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
                route_policy_id="bybit:v5:market-ticker",
                capability_snapshot_id="cap-1",
                provider_qualification_id="qual-1",
                adapter_build_id="build-1",
                source_revision="r1",
                source_event_at=NOW - timedelta(seconds=1),
                available_at=NOW,
                observed_at=NOW,
                freshness_rule_id="fresh-v1",
                origin_binding_id="caller-origin",
                query_digest=Q,
                response_sha256=R,
                evidence_class="PROVIDER_ORIGIN",
                instrument_version="BTCUSDT@1",
                mark=Decimal("100"),
            )

    def test_provider_environment_changes_content_and_scope_identity(self):
        testnet = mark(provider_environment="TESTNET")
        demo = mark(provider_environment="DEMO")
        self.assertNotEqual(testnet.scope_id, demo.scope_id)
        self.assertNotEqual(testnet.observation_id, demo.observation_id)

    def test_content_derived_idempotence_and_explicit_supersession(self):
        with TemporaryDirectory() as directory:
            book = DurableValuationBook(JournalStore(f"{directory}/journal.sqlite3"))
            first = mark()
            self.assertTrue(book.record(first))
            self.assertFalse(book.record(first))

            changed = mark(value="101")
            with self.assertRaisesRegex(
                ValuationConflict,
                "explicit supersession",
            ):
                book.record(changed)

            corrected = mark(
                value="101",
                available_at=NOW + timedelta(seconds=1),
                observed_at=NOW + timedelta(seconds=1),
                supersedes=first.observation_id,
            )
            self.assertTrue(book.record(corrected))
            self.assertFalse(book.record(corrected))

    def test_correction_graph_cannot_branch_from_one_predecessor(self):
        with TemporaryDirectory() as directory:
            book = DurableValuationBook(JournalStore(f"{directory}/journal.sqlite3"))
            first = mark()
            self.assertTrue(book.record(first))
            winner = mark(
                value="101",
                available_at=NOW + timedelta(seconds=1),
                observed_at=NOW + timedelta(seconds=1),
                supersedes=first.observation_id,
            )
            self.assertTrue(book.record(winner))
            loser = mark(
                value="102",
                available_at=NOW + timedelta(seconds=2),
                observed_at=NOW + timedelta(seconds=2),
                supersedes=first.observation_id,
            )
            with self.assertRaisesRegex(
                ValuationConflict,
                "one current observation",
            ):
                book.record(loser)


    def test_replay_rejects_future_predecessor_even_when_graph_is_complete(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableValuationBook(store)
            first = mark()
            self.assertTrue(book.record(first))

            future_predecessor = mark(
                value="101",
                available_at=NOW + timedelta(seconds=2),
                observed_at=NOW + timedelta(seconds=2),
                supersedes=first.observation_id,
            )
            premature = mark(
                value="102",
                available_at=NOW + timedelta(seconds=3),
                observed_at=NOW + timedelta(seconds=3),
                supersedes=future_predecessor.observation_id,
            )
            append_raw_valuation(
                store,
                book,
                premature,
                aggregate_version=2,
            )
            append_raw_valuation(
                store,
                book,
                future_predecessor,
                aggregate_version=3,
            )

            restarted = DurableValuationBook(JournalStore(f"{directory}/journal.sqlite3"))
            with self.assertRaisesRegex(
                ValuationConflict,
                "one current observation",
            ):
                restarted.resolve_at(
                    journal_sequence_cut=store.current_journal_sequence(),
                    as_of=NOW + timedelta(seconds=4),
                    kind="MARK",
                    provider_id="BYBIT",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    route_policy_id="bybit:v5:market-ticker",
                    instrument_version="BTCUSDT@1",
                    require_production=False,
                )

    def test_replay_rejects_correction_with_regressed_availability(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableValuationBook(store)
            first = mark(
                available_at=NOW + timedelta(seconds=2),
                observed_at=NOW + timedelta(seconds=2),
            )
            self.assertTrue(book.record(first))
            regressed = mark(
                value="101",
                available_at=NOW + timedelta(seconds=1),
                observed_at=NOW + timedelta(seconds=3),
                supersedes=first.observation_id,
            )
            append_raw_valuation(store, book, regressed, aggregate_version=2)

            restarted = DurableValuationBook(JournalStore(f"{directory}/journal.sqlite3"))
            with self.assertRaisesRegex(
                ValuationConflict,
                "availability must advance",
            ):
                restarted.resolve_at(
                    journal_sequence_cut=store.current_journal_sequence(),
                    as_of=NOW + timedelta(seconds=4),
                    kind="MARK",
                    provider_id="BYBIT",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    route_policy_id="bybit:v5:market-ticker",
                    instrument_version="BTCUSDT@1",
                    require_production=False,
                )

    def test_replay_rejects_correction_with_regressed_observation_time(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableValuationBook(store)
            first = mark(
                available_at=NOW,
                observed_at=NOW + timedelta(seconds=2),
            )
            self.assertTrue(book.record(first))
            regressed = mark(
                value="101",
                available_at=NOW + timedelta(seconds=1),
                observed_at=NOW + timedelta(seconds=1),
                supersedes=first.observation_id,
            )
            append_raw_valuation(store, book, regressed, aggregate_version=2)

            restarted = DurableValuationBook(JournalStore(f"{directory}/journal.sqlite3"))
            with self.assertRaisesRegex(
                ValuationConflict,
                "observation cannot move backward",
            ):
                restarted.resolve_at(
                    journal_sequence_cut=store.current_journal_sequence(),
                    as_of=NOW + timedelta(seconds=4),
                    kind="MARK",
                    provider_id="BYBIT",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    route_policy_id="bybit:v5:market-ticker",
                    instrument_version="BTCUSDT@1",
                    require_production=False,
                )

    def test_provider_environment_scope_never_cross_satisfies(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableValuationBook(store)
            testnet = mark(provider_environment="TESTNET")
            self.assertTrue(book.record(testnet))
            cut = store.current_journal_sequence()
            selected = book.resolve_at(
                journal_sequence_cut=cut,
                as_of=NOW,
                kind="MARK",
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
                route_policy_id="bybit:v5:market-ticker",
                instrument_version="BTCUSDT@1",
                require_production=False,
            )
            self.assertEqual(selected.observation_id, testnet.observation_id)
            with self.assertRaisesRegex(
                ValuationError,
                "no valuation observation",
            ):
                book.resolve_at(
                    journal_sequence_cut=cut,
                    as_of=NOW,
                    kind="MARK",
                    provider_id="BYBIT",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_environment="DEMO",
                    route_policy_id="bybit:v5:market-ticker",
                    instrument_version="BTCUSDT@1",
                    require_production=False,
                )

    def test_restart_reconstructs_same_historical_and_current_identity(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            book = DurableValuationBook(store)
            first = mark()
            self.assertTrue(book.record(first))
            first_cut = store.current_journal_sequence()
            corrected = mark(
                value="101",
                available_at=NOW + timedelta(seconds=1),
                observed_at=NOW + timedelta(seconds=1),
                supersedes=first.observation_id,
            )
            self.assertTrue(book.record(corrected))
            current_cut = store.current_journal_sequence()

            restarted = DurableValuationBook(JournalStore(path))
            historical = restarted.resolve_at(
                journal_sequence_cut=first_cut,
                as_of=NOW + timedelta(seconds=2),
                kind="MARK",
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
                route_policy_id="bybit:v5:market-ticker",
                instrument_version="BTCUSDT@1",
                require_production=False,
            )
            current = restarted.resolve_at(
                journal_sequence_cut=current_cut,
                as_of=NOW + timedelta(seconds=2),
                kind="MARK",
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
                route_policy_id="bybit:v5:market-ticker",
                instrument_version="BTCUSDT@1",
                require_production=False,
            )
            self.assertEqual(historical.observation_id, first.observation_id)
            self.assertEqual(current.observation_id, corrected.observation_id)
            self.assertEqual(
                observation_set_digest((historical, current)),
                observation_set_digest((current, historical)),
            )

    def test_later_correction_cannot_rewrite_historical_journal_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableValuationBook(store)
            first = mark()
            self.assertTrue(book.record(first))
            first_cut = store.current_journal_sequence()

            corrected = mark(
                value="101",
                available_at=NOW + timedelta(seconds=1),
                observed_at=NOW + timedelta(seconds=1),
                supersedes=first.observation_id,
            )
            self.assertTrue(book.record(corrected))
            corrected_cut = store.current_journal_sequence()

            historical = book.resolve_at(
                journal_sequence_cut=first_cut,
                as_of=NOW + timedelta(seconds=2),
                kind="MARK",
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
                route_policy_id="bybit:v5:market-ticker",
                instrument_version="BTCUSDT@1",
                require_production=False,
            )
            current = book.resolve_at(
                journal_sequence_cut=corrected_cut,
                as_of=NOW + timedelta(seconds=2),
                kind="MARK",
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
                route_policy_id="bybit:v5:market-ticker",
                instrument_version="BTCUSDT@1",
                require_production=False,
            )
            self.assertEqual(historical.observation_id, first.observation_id)
            self.assertEqual(current.observation_id, corrected.observation_id)

    def test_freshness_evidence_uses_registry_issued_policy_and_exact_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            resolved = resolved_policy(store, data_age="2")
            observation = mark(
                source_event_at=NOW,
                available_at=NOW,
                observed_at=NOW,
            )
            evidence = evaluate_valuation_freshness(
                observation,
                resolved,
                as_of=NOW + timedelta(seconds=2),
                journal_sequence_cut=resolved.resolved_journal_sequence_cut,
            )
            self.assertEqual(evidence.freshness_field, "max_data_age_seconds")
            self.assertEqual(evidence.max_age_seconds, Decimal("2"))
            self.assertEqual(evidence.source_age_microseconds, 2_000_000)
            self.assertEqual(evidence.policy_id, "core-risk")
            self.assertTrue(evidence.evidence_digest.startswith("sha256:"))

    def test_freshness_bridge_rejects_mutated_registry_policy(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            resolved = resolved_policy(store, data_age="2")
            object.__setattr__(
                resolved.policy,
                "max_data_age_seconds",
                Decimal("999"),
            )
            with self.assertRaisesRegex(
                ValuationError,
                "changed after registry issuance",
            ):
                evaluate_valuation_freshness(
                    mark(),
                    resolved,
                    as_of=NOW,
                    journal_sequence_cut=resolved.resolved_journal_sequence_cut,
                )

    def test_freshness_boundary_uses_exact_fractional_seconds(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            resolved = resolved_policy(store, data_age="1.5")
            observation = mark(source_event_at=NOW - timedelta(seconds=1, milliseconds=500))
            evidence = evaluate_valuation_freshness(
                observation,
                resolved,
                as_of=NOW,
                journal_sequence_cut=resolved.resolved_journal_sequence_cut,
            )
            self.assertEqual(evidence.source_age_microseconds, 1_500_000)

            too_old = mark(
                source_event_at=NOW - timedelta(seconds=1, microseconds=500001)
            )
            with self.assertRaisesRegex(ValuationError, "stale"):
                evaluate_valuation_freshness(
                    too_old,
                    resolved,
                    as_of=NOW,
                    journal_sequence_cut=resolved.resolved_journal_sequence_cut,
                )

    def test_freshness_evidence_rejects_stale_mark_future_and_cut_mismatch(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            resolved = resolved_policy(store, data_age="1")
            observation = mark()
            with self.assertRaisesRegex(ValuationError, "stale"):
                evaluate_valuation_freshness(
                    observation,
                    resolved,
                    as_of=NOW + timedelta(seconds=2),
                    journal_sequence_cut=resolved.resolved_journal_sequence_cut,
                )
            with self.assertRaisesRegex(ValuationError, "same journal sequence cut"):
                evaluate_valuation_freshness(
                    observation,
                    resolved,
                    as_of=NOW,
                    journal_sequence_cut=resolved.resolved_journal_sequence_cut - 1,
                )
            future = mark(
                source_event_at=NOW + timedelta(seconds=1),
                available_at=NOW + timedelta(seconds=1),
                observed_at=NOW + timedelta(seconds=1),
            )
            with self.assertRaisesRegex(ValuationError, "future"):
                evaluate_valuation_freshness(
                    future,
                    resolved,
                    as_of=NOW,
                    journal_sequence_cut=resolved.resolved_journal_sequence_cut,
                )

    def test_freshness_evidence_identity_changes_with_activated_policy(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            registry = DurableRiskPolicyRegistry(store)
            exact_scope = risk_scope()
            registry.register(
                scope=exact_scope,
                policy_id="core-risk",
                version=1,
                policy=risk_policy(data_age="2"),
                committed_at=NOW,
            )
            registry.activate(
                scope=exact_scope,
                policy_id="core-risk",
                version=1,
                committed_at=NOW + timedelta(milliseconds=1),
            )
            first_policy = registry.resolve_current(exact_scope)
            observation = mark()
            first = evaluate_valuation_freshness(
                observation,
                first_policy,
                as_of=NOW,
                journal_sequence_cut=first_policy.resolved_journal_sequence_cut,
            )

            registry.register(
                scope=exact_scope,
                policy_id="core-risk",
                version=2,
                policy=risk_policy(data_age="3"),
                committed_at=NOW + timedelta(milliseconds=2),
            )
            registry.activate(
                scope=exact_scope,
                policy_id="core-risk",
                version=2,
                committed_at=NOW + timedelta(milliseconds=3),
            )
            second_policy = registry.resolve_current(exact_scope)
            second = evaluate_valuation_freshness(
                observation,
                second_policy,
                as_of=NOW,
                journal_sequence_cut=second_policy.resolved_journal_sequence_cut,
            )

            self.assertNotEqual(first.policy_content_digest, second.policy_content_digest)
            self.assertNotEqual(first.evidence_digest, second.evidence_digest)
            self.assertEqual(first.observation_id, second.observation_id)

    def test_fx_freshness_uses_fx_policy_bound_and_scope_mismatch_fails(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            resolved = resolved_policy(store, fx_age="3")
            fx = diagnostic_fx_observation(
                provider_id="BYBIT",
                account_id="acct-1",
                environment="PAPER",
                provider_environment="TESTNET",
                route_policy_id="bybit:v5:market-ticker",
                capability_snapshot_id="cap-fx-1",
                provider_qualification_id="qual-fx-1",
                adapter_build_id="build-fx-1",
                source_revision="fx-r1",
                source_event_at=NOW - timedelta(seconds=3),
                available_at=NOW,
                observed_at=NOW,
                freshness_rule_id="risk-policy:fx-age-v1",
                origin_binding_id="diagnostic-fx-origin",
                query_digest=Q,
                response_sha256=R,
                base_currency="EUR",
                quote_currency="USD",
                bid=Decimal("1.10"),
                ask=Decimal("1.11"),
            )
            evidence = evaluate_valuation_freshness(
                fx,
                resolved,
                as_of=NOW,
                journal_sequence_cut=resolved.resolved_journal_sequence_cut,
            )
            self.assertEqual(evidence.freshness_field, "max_fx_age_seconds")
            self.assertEqual(evidence.max_age_seconds, Decimal("3"))

            wrong_scope = diagnostic_fx_observation(
                provider_id="BYBIT",
                account_id="other-account",
                environment="PAPER",
                provider_environment="TESTNET",
                route_policy_id="bybit:v5:market-ticker",
                capability_snapshot_id="cap-fx-1",
                provider_qualification_id="qual-fx-1",
                adapter_build_id="build-fx-1",
                source_revision="fx-r1",
                source_event_at=NOW,
                available_at=NOW,
                observed_at=NOW,
                freshness_rule_id="risk-policy:fx-age-v1",
                origin_binding_id="diagnostic-fx-origin",
                query_digest=Q,
                response_sha256=R,
                base_currency="EUR",
                quote_currency="USD",
                bid=Decimal("1.10"),
                ask=Decimal("1.11"),
            )
            with self.assertRaisesRegex(ValuationError, "scope do not match"):
                evaluate_valuation_freshness(
                    wrong_scope,
                    resolved,
                    as_of=NOW,
                    journal_sequence_cut=resolved.resolved_journal_sequence_cut,
                )

    def test_resolve_fresh_at_rejects_same_sequence_from_different_store(self):
        with TemporaryDirectory() as directory:
            valuation_store = JournalStore(f"{directory}/valuation.sqlite3")
            policy_store = JournalStore(f"{directory}/policy.sqlite3")
            book = DurableValuationBook(valuation_store)
            self.assertTrue(book.record(mark()))

            registry = DurableRiskPolicyRegistry(policy_store)
            exact_scope = risk_scope()
            registry.register(
                scope=exact_scope,
                policy_id="core-risk",
                version=1,
                policy=risk_policy(),
                committed_at=NOW,
            )
            registry.activate(
                scope=exact_scope,
                policy_id="core-risk",
                version=1,
                committed_at=NOW + timedelta(milliseconds=1),
            )
            policy_cut = policy_store.current_journal_sequence()
            resolved = registry.resolve_current(
                exact_scope,
                journal_sequence_cut=policy_cut,
            )
            while valuation_store.current_journal_sequence() < policy_cut:
                next_sequence = valuation_store.current_journal_sequence() + 1
                payload = {"value": f"padding-{next_sequence}"}
                valuation_store.append_event(
                    {
                        "event_id": f"padding-{next_sequence}",
                        "event_type": "Padding.v1",
                        "aggregate_type": "padding",
                        "aggregate_id": f"padding-{next_sequence}",
                        "aggregate_version": "1",
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                        "committed_at": NOW.isoformat().replace("+00:00", "Z"),
                    }
                )
            self.assertEqual(
                valuation_store.current_journal_sequence(),
                policy_cut,
            )
            with self.assertRaisesRegex(
                ValuationError,
                "same JournalStore generation",
            ):
                book.resolve_fresh_at(
                    journal_sequence_cut=policy_cut,
                    as_of=NOW,
                    policy=resolved,
                    kind="MARK",
                    provider_id="BYBIT",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    route_policy_id="bybit:v5:market-ticker",
                    instrument_version="BTCUSDT@1",
                )

    def test_resolve_fresh_at_never_promotes_diagnostic_observation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableValuationBook(store)
            observation = mark()
            self.assertTrue(book.record(observation))

            registry = DurableRiskPolicyRegistry(store)
            exact_scope = risk_scope()
            registry.register(
                scope=exact_scope,
                policy_id="core-risk",
                version=1,
                policy=risk_policy(data_age="5"),
                committed_at=NOW,
            )
            registry.activate(
                scope=exact_scope,
                policy_id="core-risk",
                version=1,
                committed_at=NOW + timedelta(milliseconds=1),
            )
            cut = store.current_journal_sequence()
            resolved = registry.resolve_current(
                exact_scope,
                journal_sequence_cut=cut,
            )
            with self.assertRaisesRegex(
                ValuationError,
                "PROVIDER_ORIGIN",
            ):
                book.resolve_fresh_at(
                    journal_sequence_cut=cut,
                    as_of=NOW,
                    policy=resolved,
                    kind="MARK",
                    provider_id="BYBIT",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    route_policy_id="bybit:v5:market-ticker",
                    instrument_version="BTCUSDT@1",
                )

    def test_diagnostic_history_never_satisfies_production_resolver(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableValuationBook(store)
            observation = mark()
            book.record(observation)
            cut = store.current_journal_sequence()
            with self.assertRaisesRegex(
                ValuationError,
                "PROVIDER_ORIGIN",
            ):
                book.resolve_at(
                    journal_sequence_cut=cut,
                    as_of=NOW,
                    kind="MARK",
                    provider_id="BYBIT",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    route_policy_id="bybit:v5:market-ticker",
                    instrument_version="BTCUSDT@1",
                )

    def test_fx_scope_and_set_digest_are_deterministic(self):
        fx = diagnostic_fx_observation(
            provider_id="SIMULATED",
            account_id="acct-1",
            environment="SIMULATION",
            provider_environment="SIMULATION",
            route_policy_id="sim:fx:v1",
            capability_snapshot_id="cap-fx-1",
            provider_qualification_id="qual-fx-1",
            adapter_build_id="build-fx-1",
            source_revision="fx-r1",
            source_event_at=NOW - timedelta(seconds=1),
            available_at=NOW,
            observed_at=NOW,
            freshness_rule_id="risk-policy:fx-age-v1",
            origin_binding_id="diagnostic-fx-origin",
            query_digest=Q,
            response_sha256=R,
            base_currency="EUR",
            quote_currency="USD",
            bid=Decimal("1.1000"),
            ask=Decimal("1.1002"),
        )
        mark_observation = mark()
        first = observation_set_digest((fx, mark_observation))
        second = observation_set_digest((mark_observation, fx))
        self.assertEqual(first, second)

    def test_copied_valid_event_cannot_cross_physical_store_generation(self):
        with TemporaryDirectory() as directory:
            source_store = JournalStore(f"{directory}/source.sqlite3")
            source_book = DurableValuationBook(source_store)
            observation = mark()
            self.assertTrue(source_book.record(observation))
            saved = source_store.load_events(
                "valuation_observation",
                observation.scope_id,
            )[0]

            target_store = JournalStore(f"{directory}/target.sqlite3")
            target_store.append_event(
                {
                    "event_id": saved["event_id"],
                    "event_type": saved["event_type"],
                    "aggregate_type": saved["aggregate_type"],
                    "aggregate_id": saved["aggregate_id"],
                    "aggregate_version": str(saved["aggregate_version"]),
                    "payload": saved["payload"],
                    "payload_hash": saved["payload_hash"],
                    "committed_at": saved["committed_at"],
                }
            )
            target_book = DurableValuationBook(target_store)
            with self.assertRaisesRegex(
                ValuationConflict,
                "different JournalStore generation",
            ):
                target_book.resolve_at(
                    journal_sequence_cut=target_store.current_journal_sequence(),
                    as_of=NOW,
                    kind="MARK",
                    provider_id="BYBIT",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    route_policy_id="bybit:v5:market-ticker",
                    instrument_version="BTCUSDT@1",
                    require_production=False,
                )

    def test_store_subclass_cannot_become_durable_valuation_authority(self):
        class DerivedStore(JournalStore):
            pass

        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                DurableValuationBook(
                    DerivedStore(f"{directory}/journal.sqlite3")
                )


    def test_book_rejects_post_construction_journal_method_shadow_before_callback(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableValuationBook(store)
            touched = []

            def poison(*_args, **_kwargs):
                touched.append("called")
                raise AssertionError("instance-shadowed journal method must not run")

            store.current_journal_sequence = poison
            with self.assertRaisesRegex(
                (RuntimeError, ValuationConflict),
                "shadow|authority|composition",
            ):
                book.resolve_at(
                    journal_sequence_cut=0,
                    as_of=NOW,
                    kind="MARK",
                    provider_id="BYBIT",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    route_policy_id="bybit:v5:market-ticker",
                    instrument_version="BTCUSDT@1",
                    require_production=False,
                )
            self.assertEqual(touched, [])

    def test_book_composition_cannot_be_reinitialized_or_retargeted(self):
        with TemporaryDirectory() as directory:
            first_store = JournalStore(f"{directory}/first.sqlite3")
            second_store = JournalStore(f"{directory}/second.sqlite3")
            book = DurableValuationBook(first_store)

            with self.assertRaisesRegex(
                ValuationConflict,
                "already initialized",
            ):
                DurableValuationBook.__init__(book, second_store)

            book.store = second_store
            with self.assertRaisesRegex(
                ValuationConflict,
                "composition changed",
            ):
                book.resolve_at(
                    journal_sequence_cut=0,
                    as_of=NOW,
                    kind="MARK",
                    provider_id="BYBIT",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    route_policy_id="bybit:v5:market-ticker",
                    instrument_version="BTCUSDT@1",
                    require_production=False,
                )

    def test_replay_rejects_mapping_subclass_before_mapping_callbacks(self):
        class HostileMapping(dict):
            touched = False

            def get(self, *_args, **_kwargs):
                type(self).touched = True
                raise AssertionError("mapping subclass callback must not run")

        with self.assertRaisesRegex(
            ValuationConflict,
            "payload",
        ):
            from mvp.autotrade_mvp import valuation_authority as authority
            authority._observation_from_payload(HostileMapping())
        self.assertFalse(HostileMapping.touched)

    def test_post_construction_observation_mutation_fails_before_journal_write(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableValuationBook(store)
            observation = mark()
            object.__setattr__(observation, "mark", Decimal("999"))
            before = store.current_journal_sequence()
            with self.assertRaisesRegex(
                ValuationConflict,
                "identity|content|payload",
            ):
                book.record(observation)
            self.assertEqual(store.current_journal_sequence(), before)

    def test_raw_provider_origin_event_cannot_mint_production_replay_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableValuationBook(store)
            forged = mark().to_contract_dict()
            forged["evidence_class"] = "PROVIDER_ORIGIN"
            body = {
                key: value
                for key, value in forged.items()
                if key not in {"observation_id", "scope_id"}
            }
            forged["observation_id"] = "valuation:" + payload_digest(body)
            durable_payload = {
                "schema_version": "1.0.0",
                "store_identity": authority._store_identity_payload(book.store_identity),
                "store_identity_digest": book.store_identity_digest,
                "observation": forged,
            }
            store.append_event(
                {
                    "event_id": forged["observation_id"],
                    "event_type": "ValuationObservation.v1",
                    "aggregate_type": "valuation_observation",
                    "aggregate_id": forged["scope_id"],
                    "aggregate_version": "1",
                    "payload": durable_payload,
                    "payload_hash": payload_digest(durable_payload),
                    "committed_at": NOW.isoformat().replace("+00:00", "Z"),
                }
            )

            with self.assertRaisesRegex(
                ValuationConflict,
                "PROVIDER_ORIGIN replay requires integrated",
            ):
                book.resolve_at(
                    journal_sequence_cut=store.current_journal_sequence(),
                    as_of=NOW,
                    kind="MARK",
                    provider_id="BYBIT",
                    account_id="acct-1",
                    environment="PAPER",
                    provider_environment="TESTNET",
                    route_policy_id="bybit:v5:market-ticker",
                    instrument_version="BTCUSDT@1",
                )

if __name__ == "__main__":
    unittest.main()
