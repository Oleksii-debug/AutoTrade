"""Zero-wire risk rejection recovery from the canonical durable authorities."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import simulation_session as simulation
from mvp.autotrade_mvp.persistence import JournalStore

NOW = "2026-10-03T19:00:00Z"
PRICES = ["1000", "1001", "1003"]
EPISODE = "rejected-recovery"


class RiskRejectedRecoveryTests(unittest.TestCase):
    def crash(self):
        real = simulation._event
        def event(store, kind, episode, payload, now, **kwargs):
            if kind == "SimulationSessionCompleted":
                raise RuntimeError("crash-before-completion")
            return real(store, kind, episode, payload, now, **kwargs)
        return patch.object(simulation, "_event", new=event)

    def run_episode(self, root):
        return simulation.run_canonical_simulation(PRICES, root, episode_id=EPISODE, now=NOW)

    def interrupted(self, root):
        with self.crash(), self.assertRaisesRegex(RuntimeError, "crash-before-completion"):
            self.run_episode(root)
        return JournalStore(Path(root) / "journal.sqlite3")

    def foreign_command(self, store):
        store.record_command(command_id="foreign", actor="foreign",
                             environment="SIMULATION", idempotency_key="foreign",
                             request={"operation": "foreign"}, result={"ok": True}, state_version=0)

    def test_uninterrupted_equals_crash_restart_and_repeat(self):
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            uninterrupted = self.run_episode(first)
            store = self.interrupted(second)
            before = store.whole_store_state_cut()
            with patch.object(simulation.AuthorityService, "admit", side_effect=AssertionError("no readmission")), patch.object(simulation, "SimulatedProvider", side_effect=AssertionError("no new provider")):
                recovered = self.run_episode(second)
                repeated = self.run_episode(second)
            self.assertEqual(uninterrupted["status"], "RISK_REJECTED")
            self.assertFalse(uninterrupted.pop("resumed"))
            self.assertTrue(recovered.pop("resumed"))
            self.assertTrue(repeated.pop("resumed"))
            self.assertEqual(uninterrupted, recovered)
            self.assertEqual(recovered, repeated)
            baseline = JournalStore(Path(first) / "journal.sqlite3")
            for kind in ("canonical_simulation_session", "authority_state", "risk_decision", "economic_book", "account_reconciliation", "submission_attempt", "reservation_book"):
                self.assertEqual(baseline.load_events_by_aggregate_type(kind), store.load_events_by_aggregate_type(kind))
            self.assertEqual(store.whole_store_state_cut(), baseline.whole_store_state_cut())
            self.assertEqual(before["journal_sequence"] + 1, store.current_journal_sequence())

    def test_foreign_command_after_rejection_prevents_terminal_write(self):
        with TemporaryDirectory() as root:
            store = self.interrupted(root)
            self.foreign_command(store)
            before = store.whole_store_state_cut()
            with self.assertRaisesRegex(ValueError, "durable state is not exact"):
                self.run_episode(root)
            self.assertEqual(store.whole_store_state_cut(), before)

    def test_cas_rejects_foreign_command_at_terminal_boundary(self):
        with TemporaryDirectory() as root:
            store = self.interrupted(root)
            real = simulation._event
            def racing(store, kind, episode, payload, now, **kwargs):
                if kind == "SimulationSessionCompleted":
                    self.foreign_command(store)
                return real(store, kind, episode, payload, now, **kwargs)
            with patch.object(simulation, "_event", new=racing), self.assertRaises(ValueError):
                self.run_episode(root)
            self.assertEqual(len(store.load_events_by_aggregate_type("canonical_simulation_session")), 1)

    def test_completed_rejection_cannot_assert_an_unrecorded_reconciliation(self):
        with TemporaryDirectory() as root:
            store = self.interrupted(root)
            result, _ = simulation._risk_rejected_projection(store, episode_id=EPISODE, completed=False)
            result["reconciliation_event_id"] = "forged"
            simulation._event(store, "SimulationSessionCompleted", EPISODE, result, NOW)
            with self.assertRaisesRegex(ValueError, "differs from durable zero-wire facts"):
                self.run_episode(root)

    def test_completed_unknown_is_not_accepted_as_terminal_success(self):
        with TemporaryDirectory() as root:
            store = self.interrupted(root)
            result, _ = simulation._risk_rejected_projection(store, episode_id=EPISODE, completed=False)
            result["status"] = "UNKNOWN"
            simulation._event(store, "SimulationSessionCompleted", EPISODE, result, NOW)
            with self.assertRaisesRegex(ValueError, "unsupported completed simulation outcome"):
                self.run_episode(root)

    def test_simulation_policy_clock_is_scoped_and_validated_before_mutation(self):
        from dataclasses import replace
        from mvp.autotrade_mvp.authority import AuthorityPolicy, AuthorityService
        policy = AuthorityPolicy.create(
            policy_id="clock-policy", account_id=simulation.ACCOUNT,
            environments={"SIMULATION"}, instruments={(simulation.INSTRUMENT_ID, 1)},
            actions={"ORDER.SUBMIT"}, max_notional="1000",
            expires_at="2026-10-04T00:00:00Z", autonomous=True,
            protection_only=False, version=1,
        )
        with TemporaryDirectory() as root:
            store = JournalStore(Path(root) / "journal.sqlite3")
            service = AuthorityService(store)
            for bad in (123, "bad", "2026-10-03T19:00:00"):
                with self.subTest(value=bad), self.assertRaises((ValueError, TypeError)):
                    service.register_policy(policy, simulation_time=bad)
                self.assertEqual(store.current_journal_sequence(), 0)
            for environments in ({"PAPER"}, {"LIVE"}, {"SIMULATION", "LIVE"}):
                with self.subTest(environments=environments), self.assertRaisesRegex(ValueError, "SIMULATION-only"):
                    service.register_policy(replace(policy, environments=frozenset(environments)), simulation_time=NOW)
                self.assertEqual(store.current_journal_sequence(), 0)
            self.assertTrue(service.register_policy(policy, simulation_time="2026-10-03T21:00:00+02:00"))
            event = store.load_events("authority_state", "canonical")[0]
            self.assertEqual(event["committed_at"], NOW)
            self.assertFalse(service.register_policy(policy, simulation_time=NOW))
            self.assertEqual(store.current_journal_sequence(), 1)

    def test_started_without_rejection_remains_unknown_without_resend(self):
        with TemporaryDirectory() as root:
            with patch.object(simulation.AuthorityService, "admit", side_effect=RuntimeError("crash-before-admission")), self.assertRaises(RuntimeError):
                self.run_episode(root)
            with patch.object(simulation.AuthorityService, "admit", side_effect=AssertionError("no readmission")), patch.object(simulation, "SimulatedProvider", side_effect=AssertionError("no provider")):
                result = self.run_episode(root)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["new_outbound_requests"], 0)


if __name__ == "__main__":
    unittest.main()
