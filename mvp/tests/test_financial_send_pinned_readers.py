from types import SimpleNamespace
import unittest

from mvp.autotrade_mvp.financial_send_authority import (
    _require_binding_matches_durable_admission,
)


class _PinnedHistoricalReaderUsed(RuntimeError):
    pass


class _PinnedJournalReaderUsed(RuntimeError):
    pass


class FinancialSendPinnedReaderTests(unittest.TestCase):
    def test_instance_shadow_cannot_replace_pinned_historical_admission_reader(self):
        callbacks = {"shadow": 0}

        class Service:
            pass

        service = Service()

        def shadow_historical(_admission_id):
            callbacks["shadow"] += 1
            return {"outcome": "ADMITTED"}

        service.historical_admission = shadow_historical
        journal = object()

        def pinned_historical(bound_service, admission_id):
            self.assertIs(bound_service, service)
            self.assertEqual(admission_id, "admission-1")
            raise _PinnedHistoricalReaderUsed("pinned historical reader executed")

        def unused_load_events(*_args):
            raise AssertionError("risk reader must not run after historical sentinel")

        with self.assertRaisesRegex(
            _PinnedHistoricalReaderUsed,
            "pinned historical reader executed",
        ):
            _require_binding_matches_durable_admission(
                service=service,
                journal=journal,
                historical_admission=pinned_historical,
                load_events=unused_load_events,
                admission_id="admission-1",
                intent_hash="intent-hash-1",
                action="CREATE",
                binding=object(),
            )

        self.assertEqual(callbacks["shadow"], 0)

    def test_instance_shadow_cannot_replace_pinned_risk_event_reader(self):
        callbacks = {"historical_shadow": 0, "journal_shadow": 0}

        class Service:
            pass

        class Journal:
            pass

        service = Service()
        journal = Journal()

        def shadow_historical(_admission_id):
            callbacks["historical_shadow"] += 1
            raise AssertionError("instance historical shadow executed")

        def shadow_load_events(_aggregate_type, _aggregate_id):
            callbacks["journal_shadow"] += 1
            raise AssertionError("instance journal shadow executed")

        service.historical_admission = shadow_historical
        journal.load_events = shadow_load_events

        binding = SimpleNamespace(
            account_id="account-1",
            runtime_environment="PAPER",
            instrument_id="00000000-0000-0000-0000-000000000101",
            instrument_version=7,
            risk_decision_id="risk:sha256:" + "2" * 64,
            reservation_id="reservation-1",
            capability_snapshot_id="capability-1",
            admitted_journal_sequence_cut=41,
        )

        def pinned_historical(bound_service, admission_id):
            self.assertIs(bound_service, service)
            self.assertEqual(admission_id, "admission-1")
            return {
                "outcome": "ADMITTED",
                "intent_hash": "intent-hash-1",
                "account_id": "account-1",
                "environment": "PAPER",
                "instrument": {
                    "instrument_id": "00000000-0000-0000-0000-000000000101",
                    "version": 7,
                },
                "action": "CREATE",
                "risk_decision_id": binding.risk_decision_id,
                "reservation_id": "reservation-1",
                "capability_snapshot_id": "capability-1",
            }

        def pinned_load_events(bound_journal, aggregate_type, aggregate_id):
            self.assertIs(bound_journal, journal)
            self.assertEqual(aggregate_type, "risk_decision")
            self.assertEqual(aggregate_id, binding.risk_decision_id)
            raise _PinnedJournalReaderUsed("pinned journal reader executed")

        with self.assertRaisesRegex(
            _PinnedJournalReaderUsed,
            "pinned journal reader executed",
        ):
            _require_binding_matches_durable_admission(
                service=service,
                journal=journal,
                historical_admission=pinned_historical,
                load_events=pinned_load_events,
                admission_id="admission-1",
                intent_hash="intent-hash-1",
                action="CREATE",
                binding=binding,
            )

        self.assertEqual(callbacks["historical_shadow"], 0)
        self.assertEqual(callbacks["journal_shadow"], 0)


if __name__ == "__main__":
    unittest.main()
