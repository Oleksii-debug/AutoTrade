from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier, Lock, get_ident
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.alpaca_options as alpaca_options_module

from mvp.autotrade_mvp.alpaca import AlpacaAdapterError, AlpacaOrderIntent
from mvp.autotrade_mvp.alpaca_options import (
    AlpacaOptionAccountEvidence,
    attach_polled_option_activity,
    classify_option_lifecycle_evidence,
    load_option_lifecycle_obligation,
    open_provisional_option_lifecycle_obligation,
    parse_polled_option_activity,
    require_option_entitlement,
)
from mvp.autotrade_mvp.persistence import JournalStore


NOW = datetime(2026, 9, 25, 0, tzinfo=timezone.utc)


def option_intent():
    return AlpacaOrderIntent.create(
        instrument_version="AAPL_OPT:v1",
        asset_class="OPTION",
        symbol="AAPL261218C00250000",
        side="BUY",
        order_type="LIMIT",
        time_in_force="GTC",
        quantity="1",
        limit_price="5.25",
        position_intent="buy_to_open",
    )


def account(**overrides):
    values = {
        "account_id": "paper-account",
        "environment": "PAPER",
        "observed_at": NOW - timedelta(minutes=5),
        "expires_at": NOW + timedelta(minutes=5),
        "options_trading_level": 2,
        "trading_blocked": False,
        "account_blocked": False,
        "source_sha256": "a" * 64,
    }
    values.update(overrides)
    return AlpacaOptionAccountEvidence(**values)


class AlpacaOptionEvidenceTests(unittest.TestCase):
    def test_fresh_level_evidence_admits_matching_option_intent(self):
        intent = option_intent()
        self.assertIs(
            require_option_entitlement(
                intent,
                account=account(),
                account_id="paper-account",
                environment="PAPER",
                required_level=2,
                at=NOW,
            ),
            intent,
        )

    def test_stale_or_blocked_account_fails_closed(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "does not admit"):
            require_option_entitlement(
                option_intent(),
                account=account(expires_at=NOW - timedelta(minutes=1)),
                account_id="paper-account",
                environment="PAPER",
                required_level=2,
                at=NOW,
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "does not admit"):
            require_option_entitlement(
                option_intent(),
                account=account(trading_blocked=True),
                account_id="paper-account",
                environment="PAPER",
                required_level=2,
                at=NOW,
            )

    def test_entitlement_expires_before_exact_expiry_instant(self):
        evidence = account(expires_at=NOW)
        with self.assertRaisesRegex(AlpacaAdapterError, "does not admit"):
            require_option_entitlement(
                option_intent(),
                account=evidence,
                account_id="paper-account",
                environment="PAPER",
                required_level=2,
                at=NOW,
            )

    def test_insufficient_option_level_fails_closed(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "does not admit"):
            require_option_entitlement(
                option_intent(),
                account=account(options_trading_level=1),
                account_id="paper-account",
                environment="PAPER",
                required_level=2,
                at=NOW,
            )

    def test_non_option_intent_cannot_use_option_entitlement(self):
        equity = AlpacaOrderIntent.create(
            instrument_version="AAPL:v1",
            asset_class="EQUITY",
            symbol="AAPL",
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(AlpacaAdapterError, "OPTION intents"):
            require_option_entitlement(
                equity,
                account=account(),
                account_id="paper-account",
                environment="PAPER",
                required_level=1,
                at=NOW,
            )

    def test_entitlement_is_bound_to_exact_account_and_environment(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "account_id mismatch"):
            require_option_entitlement(
                option_intent(),
                account=account(),
                account_id="other-account",
                environment="PAPER",
                required_level=2,
                at=NOW,
            )
        with self.assertRaisesRegex(AlpacaAdapterError, "environment mismatch"):
            require_option_entitlement(
                option_intent(),
                account=account(environment="PAPER"),
                account_id="paper-account",
                environment="LIVE",
                required_level=2,
                at=NOW,
            )

    def test_assignment_is_parsed_from_polled_activity(self):
        result = parse_polled_option_activity(
            {
                "activity_type": "OPASN",
                "id": "assignment-1",
                "symbol": "AAPL261218C00250000",
                "date": "2026-09-24",
                "qty": "2",
            },
            account_id="paper-account",
            environment="PAPER",
            observed_at=NOW,
        )
        self.assertEqual(result.account_id, "paper-account")
        self.assertEqual(result.environment, "PAPER")
        self.assertEqual(result.activity_type, "OPTION_ASSIGNMENT")
        self.assertEqual(result.symbol, "AAPL261218C00250000")
        self.assertEqual(result.provider_activity_id, "assignment-1")
        self.assertEqual(result.signed_contracts, Decimal("-2"))
        self.assertEqual(result.quantity_unit, "OPTION_CONTRACT")

    def test_lifecycle_scope_rejects_non_brokerage_environment(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "PAPER or LIVE"):
            parse_polled_option_activity(
                {
                    "activity_type": "OPASN",
                    "id": "assignment-scope",
                    "symbol": "AAPL261218C00250000",
                    "date": "2026-09-24",
                    "qty": "2",
                },
                account_id="paper-account",
                environment="SIMULATION",
                observed_at=NOW,
            )

    def test_unknown_activity_does_not_become_assignment(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "unsupported polled"):
            parse_polled_option_activity(
                {
                    "activity_type": "FILL",
                    "id": "fill-1",
                    "symbol": "AAPL261218C00250000",
                    "date": "2026-09-24",
                },
                account_id="paper-account",
                environment="PAPER",
                observed_at=NOW,
            )


    def test_future_effective_lifecycle_activity_fails_closed(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "effective_date cannot be after observed_at date"):
            parse_polled_option_activity(
                {
                    "activity_type": "OPASN",
                    "id": "future-assignment",
                    "symbol": "AAPL261218C00250000",
                    "date": "2026-09-26",
                    "qty": "2",
                },
                account_id="paper-account",
                environment="PAPER",
                observed_at=NOW,
            )



    def test_quiet_order_stream_never_proves_option_lifecycle_absence(self):
        self.assertEqual(
            classify_option_lifecycle_evidence(
                order_stream_quiet=True,
                activity=None,
            ),
            "INCONCLUSIVE",
        )
        self.assertEqual(
            classify_option_lifecycle_evidence(
                order_stream_quiet=False,
                activity=None,
            ),
            "INCONCLUSIVE",
        )

    def test_paper_position_change_stays_provisional_across_restart(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            opened = open_provisional_option_lifecycle_obligation(
                store,
                economic_change_id="position-change-1",
                account_id="paper-account",
                environment="PAPER",
                symbol="AAPL261218C00250000",
                economic_effect_observed_at=NOW,
                host_id="host-1",
                owner_epoch="epoch-1",
            )
            self.assertEqual(opened.status, "PROVISIONAL")
            self.assertTrue(opened.unresolved)
            self.assertIsNone(opened.activity_type)

            restarted = JournalStore(path)
            recovered = load_option_lifecycle_obligation(
                restarted,
                economic_change_id="position-change-1",
                account_id="paper-account",
                environment="PAPER",
            )
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.status, "PROVISIONAL")
            self.assertEqual(recovered.symbol, "AAPL261218C00250000")
            self.assertIsNone(recovered.provider_activity_id)

    def test_next_day_activity_resolves_existing_change_idempotently(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            open_provisional_option_lifecycle_obligation(
                store,
                economic_change_id="position-change-1",
                account_id="paper-account",
                environment="PAPER",
                symbol="AAPL261218C00250000",
                economic_effect_observed_at=NOW,
                host_id="host-1",
                owner_epoch="epoch-1",
            )
            activity = parse_polled_option_activity(
                {
                    "activity_type": "OPASN",
                    "id": "assignment-delayed-1",
                    "symbol": "AAPL261218C00250000",
                    "date": "2026-09-25",
                    "qty": "2",
                },
                account_id="paper-account",
                environment="PAPER",
                observed_at=NOW + timedelta(days=1),
            )
            resolved = attach_polled_option_activity(
                store,
                economic_change_id="position-change-1",
                observation=activity,
                host_id="host-1",
                owner_epoch="epoch-1",
            )
            self.assertEqual(resolved.status, "RESOLVED")
            self.assertFalse(resolved.unresolved)
            self.assertEqual(resolved.activity_type, "OPTION_ASSIGNMENT")
            self.assertEqual(
                resolved.provider_activity_id,
                "assignment-delayed-1",
            )

            aggregate_events = store.load_events_by_aggregate_type(
                "alpaca_option_lifecycle_obligation"
            )
            before_retry = len(aggregate_events)
            exact_retry = attach_polled_option_activity(
                store,
                economic_change_id="position-change-1",
                observation=activity,
                host_id="host-1",
                owner_epoch="epoch-1",
            )
            self.assertEqual(exact_retry, resolved)
            self.assertEqual(
                len(
                    store.load_events_by_aggregate_type(
                        "alpaca_option_lifecycle_obligation"
                    )
                ),
                before_retry,
            )

            recovered = load_option_lifecycle_obligation(
                JournalStore(path),
                economic_change_id="position-change-1",
                account_id="paper-account",
                environment="PAPER",
            )
            self.assertEqual(recovered.status, "RESOLVED")
            self.assertEqual(
                recovered.provider_activity_id,
                "assignment-delayed-1",
            )
            self.assertEqual(recovered.activity_signed_contracts, Decimal("-2"))
            self.assertEqual(recovered.activity_quantity_unit, "OPTION_CONTRACT")

    def test_provider_activity_cannot_resolve_two_economic_changes(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            for change_id in ("position-change-1", "position-change-2"):
                open_provisional_option_lifecycle_obligation(
                    store,
                    economic_change_id=change_id,
                    account_id="paper-account",
                    environment="PAPER",
                    symbol="AAPL261218C00250000",
                    economic_effect_observed_at=NOW,
                    host_id="host-1",
                    owner_epoch="epoch-1",
                )
            activity = parse_polled_option_activity(
                {
                    "activity_type": "OPASN",
                    "id": "assignment-shared",
                    "symbol": "AAPL261218C00250000",
                    "date": "2026-09-25",
                    "qty": "2",
                },
                account_id="paper-account",
                environment="PAPER",
                observed_at=NOW + timedelta(days=1),
            )
            attach_polled_option_activity(
                store,
                economic_change_id="position-change-1",
                observation=activity,
                host_id="host-1",
                owner_epoch="epoch-1",
            )
            with self.assertRaisesRegex(
                AlpacaAdapterError,
                "already resolves another economic change",
            ):
                attach_polled_option_activity(
                    store,
                    economic_change_id="position-change-2",
                    observation=activity,
                    host_id="host-1",
                    owner_epoch="epoch-1",
                )
            second = load_option_lifecycle_obligation(
                store,
                economic_change_id="position-change-2",
                account_id="paper-account",
                environment="PAPER",
            )
            self.assertEqual(second.status, "PROVISIONAL")

    def test_delayed_activity_must_match_provisional_symbol(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            open_provisional_option_lifecycle_obligation(
                store,
                economic_change_id="position-change-1",
                account_id="paper-account",
                environment="PAPER",
                symbol="AAPL261218C00250000",
                economic_effect_observed_at=NOW,
                host_id="host-1",
                owner_epoch="epoch-1",
            )
            wrong_symbol = parse_polled_option_activity(
                {
                    "activity_type": "OPASN",
                    "id": "assignment-wrong-symbol",
                    "symbol": "MSFT261218C00500000",
                    "date": "2026-09-25",
                    "qty": "2",
                },
                account_id="paper-account",
                environment="PAPER",
                observed_at=NOW + timedelta(days=1),
            )
            with self.assertRaisesRegex(
                AlpacaAdapterError,
                "symbol differs",
            ):
                attach_polled_option_activity(
                    store,
                    economic_change_id="position-change-1",
                    observation=wrong_symbol,
                    host_id="host-1",
                    owner_epoch="epoch-1",
                )
            unresolved = load_option_lifecycle_obligation(
                store,
                economic_change_id="position-change-1",
                account_id="paper-account",
                environment="PAPER",
            )
            self.assertEqual(unresolved.status, "PROVISIONAL")

    def test_option_nta_date_is_preserved_without_invented_midnight(self):
        for raw_type, semantic in (
            ("OPEXC", "OPTION_EXERCISE"),
            ("OPXRC", "OPTION_EXERCISE"),
            ("OPASN", "OPTION_ASSIGNMENT"),
            ("OPEXP", "OPTION_EXPIRATION"),
        ):
            with self.subTest(activity_type=raw_type):
                result = parse_polled_option_activity(
                    {
                        "activity_type": raw_type,
                        "id": f"activity-{raw_type.lower()}",
                        "symbol": "AAPL261218C00250000",
                        "date": "2026-09-24",
                        "qty": "2" if semantic == "OPTION_ASSIGNMENT" else "-2",
                    },
                    account_id="paper-account",
                    environment="PAPER",
                    observed_at=NOW,
                )
                self.assertEqual(result.activity_type, semantic)
                self.assertEqual(result.effective_date.isoformat(), "2026-09-24")
                self.assertIsNone(result.effective_at)

    def test_option_nta_requires_canonical_date_not_trade_timestamp(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "date is required"):
            parse_polled_option_activity(
                {
                    "activity_type": "OPASN",
                    "id": "timestamp-only",
                    "symbol": "AAPL261218C00250000",
                    "transaction_time": "2026-09-24T23:59:00Z",
                },
                account_id="paper-account",
                environment="PAPER",
                observed_at=NOW,
            )
        for raw_date in ("2026-9-24", "20260924", "not-a-date"):
            with self.subTest(raw_date=raw_date), self.assertRaisesRegex(
                AlpacaAdapterError,
                "canonical YYYY-MM-DD",
            ):
                parse_polled_option_activity(
                    {
                        "activity_type": "OPEXP",
                        "id": "bad-date",
                        "symbol": "AAPL261218C00250000",
                        "date": raw_date,
                        "qty": "-2",
                    },
                    account_id="paper-account",
                    environment="PAPER",
                    observed_at=NOW,
                )

    def test_option_nta_quantity_is_provider_contract_count_with_canonical_sign(self):
        cases = (
            ("OPEXC", "-2", "OPTION_EXERCISE", Decimal("2")),
            ("OPXRC", "-3", "OPTION_EXERCISE", Decimal("3")),
            ("OPASN", "2", "OPTION_ASSIGNMENT", Decimal("-2")),
            ("OPASN", "2.0", "OPTION_ASSIGNMENT", Decimal("-2.0")),
            ("OPEXP", "-4", "OPTION_EXPIRATION", Decimal("4")),
            ("OPEXP", "5", "OPTION_EXPIRATION", Decimal("-5")),
        )
        for raw_type, provider_qty, semantic, expected_signed in cases:
            with self.subTest(activity_type=raw_type, qty=provider_qty):
                observation = parse_polled_option_activity(
                    {
                        "activity_type": raw_type,
                        "id": f"quantity-{raw_type.lower()}-{provider_qty}",
                        "symbol": "AAPL261218C00250000",
                        "date": "2026-09-24",
                        "qty": provider_qty,
                    },
                    account_id="paper-account",
                    environment="PAPER",
                    observed_at=NOW,
                )
                self.assertEqual(observation.activity_type, semantic)
                self.assertEqual(observation.signed_contracts, expected_signed)
                self.assertEqual(observation.quantity_unit, "OPTION_CONTRACT")

    def test_option_nta_quantity_must_be_exact_nonzero_whole_contract_text(self):
        invalid = (None, 2, 2.0, True, "0", "0.5", "-0.5", "2e0")
        for raw_qty in invalid:
            with self.subTest(qty=raw_qty), self.assertRaises(AlpacaAdapterError):
                parse_polled_option_activity(
                    {
                        "activity_type": "OPASN",
                        "id": "invalid-quantity",
                        "symbol": "AAPL261218C00250000",
                        "date": "2026-09-24",
                        "qty": raw_qty,
                    },
                    account_id="paper-account",
                    environment="PAPER",
                    observed_at=NOW,
                )

    def test_option_nta_quantity_sign_must_match_lifecycle_semantics(self):
        with self.assertRaisesRegex(
            AlpacaAdapterError,
            "OPTION_EXERCISE requires positive",
        ):
            parse_polled_option_activity(
                {
                    "activity_type": "OPEXC",
                    "id": "wrong-exercise-sign",
                    "symbol": "AAPL261218C00250000",
                    "date": "2026-09-24",
                    "qty": "2",
                },
                account_id="paper-account",
                environment="PAPER",
                observed_at=NOW,
            )
        with self.assertRaisesRegex(
            AlpacaAdapterError,
            "OPTION_ASSIGNMENT requires negative",
        ):
            parse_polled_option_activity(
                {
                    "activity_type": "OPASN",
                    "id": "wrong-assignment-sign",
                    "symbol": "AAPL261218C00250000",
                    "date": "2026-09-24",
                    "qty": "-2",
                },
                account_id="paper-account",
                environment="PAPER",
                observed_at=NOW,
            )

    def test_resolved_obligation_revalidates_lifecycle_quantity_sign(self):
        common = {
            "economic_change_id": "durable-sign-check",
            "account_id": "paper-account",
            "environment": "PAPER",
            "symbol": "AAPL261218C00250000",
            "economic_effect_observed_at": NOW,
            "status": "RESOLVED",
            "provider_activity_id": "durable-activity",
            "activity_source_sha256": "a" * 64,
            "activity_effective_date": NOW.date(),
            "activity_quantity_unit": "OPTION_CONTRACT",
            "activity_observed_at": NOW,
        }
        with self.assertRaisesRegex(
            AlpacaAdapterError,
            "OPTION_EXERCISE requires positive durable",
        ):
            alpaca_options_module.AlpacaOptionLifecycleObligation(
                **common,
                activity_type="OPTION_EXERCISE",
                activity_signed_contracts=Decimal("-2"),
            )
        with self.assertRaisesRegex(
            AlpacaAdapterError,
            "OPTION_ASSIGNMENT requires negative durable",
        ):
            alpaca_options_module.AlpacaOptionLifecycleObligation(
                **common,
                activity_type="OPTION_ASSIGNMENT",
                activity_signed_contracts=Decimal("2"),
            )

    def test_provider_activity_claim_is_atomic_across_two_store_instances(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            setup_store = JournalStore(path)
            for change_id in ("position-race-1", "position-race-2"):
                open_provisional_option_lifecycle_obligation(
                    setup_store,
                    economic_change_id=change_id,
                    account_id="paper-account",
                    environment="PAPER",
                    symbol="AAPL261218C00250000",
                    economic_effect_observed_at=NOW,
                    host_id="host-setup",
                    owner_epoch="epoch-setup",
                )
            observation = parse_polled_option_activity(
                {
                    "activity_type": "OPASN",
                    "id": "assignment-race",
                    "symbol": "AAPL261218C00250000",
                    "date": "2026-09-25",
                    "qty": "2",
                },
                account_id="paper-account",
                environment="PAPER",
                observed_at=NOW + timedelta(days=1),
            )

            barrier = Barrier(2)
            lock = Lock()
            synchronized_threads = set()
            original_reject = (
                alpaca_options_module._reject_reused_provider_activity
            )

            def synchronized_preflight(*args, **kwargs):
                original_reject(*args, **kwargs)
                thread_id = get_ident()
                with lock:
                    first_preflight = thread_id not in synchronized_threads
                    synchronized_threads.add(thread_id)
                if first_preflight:
                    barrier.wait(timeout=5)

            def attempt(change_id):
                worker_store = JournalStore(path)
                try:
                    result = attach_polled_option_activity(
                        worker_store,
                        economic_change_id=change_id,
                        observation=observation,
                        host_id=f"host-{change_id}",
                        owner_epoch=f"epoch-{change_id}",
                    )
                    return ("resolved", result.status)
                except AlpacaAdapterError as error:
                    return ("rejected", str(error))

            with patch.object(
                alpaca_options_module,
                "_reject_reused_provider_activity",
                synchronized_preflight,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    outcomes = tuple(
                        executor.map(
                            attempt,
                            ("position-race-1", "position-race-2"),
                        )
                    )

            self.assertEqual(
                sorted(outcome[0] for outcome in outcomes),
                ["rejected", "resolved"],
            )
            verifier = JournalStore(path)
            states = [
                load_option_lifecycle_obligation(
                    verifier,
                    economic_change_id=change_id,
                    account_id="paper-account",
                    environment="PAPER",
                )
                for change_id in ("position-race-1", "position-race-2")
            ]
            self.assertEqual(
                sorted(state.status for state in states),
                ["PROVISIONAL", "RESOLVED"],
            )
            attachments = [
                event
                for event in verifier.load_events_by_aggregate_type(
                    "alpaca_option_lifecycle_obligation"
                )
                if event.get("event_type") == "OptionLifecycleActivityAttached"
            ]
            self.assertEqual(len(attachments), 1)
            self.assertEqual(
                attachments[0]["payload"]["provider_activity_id"],
                "assignment-race",
            )


if __name__ == "__main__":
    unittest.main()
