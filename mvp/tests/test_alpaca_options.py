from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest

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
                "transaction_time": "2026-09-24T23:59:00Z",
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

    def test_lifecycle_scope_rejects_non_brokerage_environment(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "PAPER or LIVE"):
            parse_polled_option_activity(
                {
                    "activity_type": "OPASN",
                    "id": "assignment-scope",
                    "symbol": "AAPL261218C00250000",
                    "transaction_time": "2026-09-24T23:59:00Z",
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
                    "transaction_time": "2026-09-24T23:59:00Z",
                },
                account_id="paper-account",
                environment="PAPER",
                observed_at=NOW,
            )


    def test_future_effective_lifecycle_activity_fails_closed(self):
        with self.assertRaisesRegex(AlpacaAdapterError, "cannot be after observed_at"):
            parse_polled_option_activity(
                {
                    "activity_type": "OPASN",
                    "id": "future-assignment",
                    "symbol": "AAPL261218C00250000",
                    "transaction_time": "2026-09-25T00:01:00Z",
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
                    "transaction_time": "2026-09-25T00:00:00Z",
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
                    "transaction_time": "2026-09-25T00:00:00Z",
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
                    "transaction_time": "2026-09-25T00:00:00Z",
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

if __name__ == "__main__":
    unittest.main()
