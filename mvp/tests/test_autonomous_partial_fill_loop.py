"""Product-level exact partial-fill, settlement and real process-crash acceptance."""
from decimal import Decimal, Inexact, Rounded, localcontext
from datetime import datetime
from copy import deepcopy
import io
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import simulation_session as session
from mvp.autotrade_mvp import authority as authority_module
from mvp.autotrade_mvp.cli import get_economic_report, get_status, main
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.durable_settlement import DurableSettlementBook
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.simulated_provider import SimulatedProvider
from mvp.autotrade_mvp.simulation_status import inspect_canonical_simulation
from mvp.tests.test_autonomous_simulation import run as base_run, NOW
from research.autotrade_research.artifacts.store import ArtifactStore

ROOT = Path(__file__).resolve().parents[2]
PROFILE = "TWO_EQUAL_PARTIALS"
PRICES = ["100", "101", "103", "90", "110", "120", "121"]


class _ClockType(type):
    def __instancecheck__(cls, value):
        return isinstance(value, datetime)


class _NoWallClock(metaclass=_ClockType):
    fromisoformat = staticmethod(datetime.fromisoformat)

    @staticmethod
    def now(*args, **kwargs):
        raise AssertionError("simulated policy cannot read physical time")


def run(directory, prices=PRICES, **kwargs):
    return base_run(directory, prices, target_quantity="2", **kwargs)


def owners(directory):
    store = JournalStore(Path(directory) / "journal.sqlite3")
    scope = dict(provider_id=session.PROVIDER, account_id=session.ACCOUNT, environment=session.ENVIRONMENT)
    economic = DurableProviderEconomicBook(store, **scope)
    orders = DurableOrderBookProjection(store, **scope, host_id="local-simulation", owner_epoch="1")
    reservations = DurableReservationBook(store, account_id=session.ACCOUNT, environment=session.ENVIRONMENT)
    settlements = DurableSettlementBook(store, **scope, provider_environment=session.ENVIRONMENT,
        evidence_artifact_root=Path(directory) / "artifacts", evidence_artifact_store=ArtifactStore(Path(directory) / "artifacts"))
    return store, economic, orders, reservations, settlements


class AutonomousPartialFillLoopTests(unittest.TestCase):
    def test_partial_then_full_status_reads_retained_two_fill_observation(self):
        with TemporaryDirectory() as directory:
            result = run(directory, ["100", "101", "103", "102"], partial_fills=True)
            status = inspect_canonical_simulation(directory)["status"]
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(status["session_status"], "COMPLETED")
            self.assertEqual(status["completed_episodes"], 4)
            self.assertEqual(status["position"], "2")

    def test_zero_does_not_acknowledge_foreign_host_ui_publications(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            for number, topic in enumerate(
                ("autotrade.simulation.events", "ui.host-events"), start=1
            ):
                payload = {"number": number}
                store.append_event(
                    {
                        "event_id": f"zero-publication-test-{number}",
                        "event_type": "PublicationTest",
                        "aggregate_type": (
                            "canonical_autonomous_simulation"
                            if number == 1 else "zero_publication_test"
                        ),
                        "aggregate_id": "test-run" if number == 1 else f"item-{number}",
                        "aggregate_version": "1",
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                        "committed_at": "2026-09-30T12:00:00Z",
                    },
                    outbox_topic=topic,
                )
            before = store.pending_outbox(limit=1000)
            session._deliver_pending_zero_publications(store, run_id="test-run")
            self.assertEqual(
                [item["topic"] for item in store.pending_outbox(limit=1000)],
                ["ui.host-events"],
            )

            foreign = next(item for item in before if item["topic"] == "ui.host-events")
            store.mark_outbox_delivered(
                foreign["outbox_id"], expected_envelope_hash=foreign["envelope_hash"]
            )
            session._deliver_pending_zero_publications(store, run_id="test-run")
            self.assertEqual(store.pending_outbox_count(), 0)

    def test_each_partial_atomically_consumes_only_its_cash_and_creates_its_settlement(self):
        with TemporaryDirectory() as directory, patch.object(session, "INITIAL_CASH", Decimal("500")):
            original = session.commit_order_fill_with_reservation_consumption
            intermediate = []

            def capture(*args, **kwargs):
                result = original(*args, **kwargs)
                store, economic, orders, reservations, settlements = owners(directory)
                intermediate.append((economic.cash("USD"), economic.position(session.INSTRUMENT),
                    orders.order(kwargs["client_order_id"]).state,
                    reservations.get(kwargs["reservation_id"]).remaining["CASH:USD"],
                    len(settlements.obligations)))
                return result

            with patch.object(session, "commit_order_fill_with_reservation_consumption", capture):
                run(directory, PRICES, stop_after_episodes=4, execution_profile=PROFILE)
            self.assertEqual(intermediate, [
                (Decimal("396.897"), Decimal("1"), "PARTIALLY_FILLED", Decimal("103.103"), 1),
                (Decimal("293.794"), Decimal("2"), "FILLED", Decimal("0"), 2),
                (Decimal("383.704"), Decimal("1"), "PARTIALLY_FILLED", Decimal("0.09"), 3),
                (Decimal("473.614"), Decimal("0"), "FILLED", Decimal("0"), 4),
            ])
            report = get_economic_report(directory)
            self.assertEqual(report["cash_buckets"], {
                "currency": "USD", "account_cash": "473.614", "settled_cash": "500",
                "unsettled_receivable": "179.82", "unsettled_payable": "206.206",
                "reserved_cash": "0", "available_cash": "293.794",
            })
            self.assertEqual((report["realized_pnl"], report["total_fees"], report["net_pnl"]),
                             ("-26", "0.386", "-26.386"))
            with localcontext() as context:
                context.prec = 2
                context.traps[Inexact] = context.traps[Rounded] = True
                self.assertEqual(get_economic_report(directory), report)
            run(directory, PRICES, execution_profile=PROFILE)
            store, economic, orders, reservations, settlements = owners(directory)
            self.assertEqual(len(settlements.obligations), 6)
            self.assertEqual(len(settlements.settled_obligation_evidence), 4)
            # The settled balance alone can fund the episode-6 buy at 120;
            # the sale settles only in episode 7. 500 - 206.206 + 179.82 - 240.24.
            self.assertEqual(get_economic_report(directory)["cash_buckets"]["available_cash"], "233.374")

    def test_hard_exit_after_first_buy_or_sell_partial_resumes_without_resend(self):
        for episode in (3, 4):
            with self.subTest(episode=episode), TemporaryDirectory(prefix="Часткове виконання ") as directory, TemporaryDirectory() as reference:
                expected = run(reference, PRICES, stop_after_episodes=episode, execution_profile=PROFILE)
                if episode == 4:
                    run(directory, PRICES, stop_after_episodes=3, execution_profile=PROFILE)
                program = '''
import os, sys
from mvp.autotrade_mvp import simulation_session as session
original = session.commit_order_fill_with_reservation_consumption
def crash(*args, **kwargs):
    result = original(*args, **kwargs)
    if kwargs["order_event_key"].endswith(":" + sys.argv[2] + ":part:1:fill"):
        os._exit(23)
    return result
session.commit_order_fill_with_reservation_consumption = crash
session.run_autonomous_simulation(["100", "101", "103", "90", "110", "120", "121"], sys.argv[1],
    run_id="acceptance", now="2026-10-03T00:00:00Z", execution_profile="TWO_EQUAL_PARTIALS", target_quantity="2")
'''
                env = dict(os.environ, PYTHONPATH=str(ROOT / "research") + os.pathsep + str(ROOT))
                child = subprocess.run([sys.executable, "-c", program, directory, str(episode)],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(child.returncode, 23, child.stderr)
                store, economic, orders, reservations, settlements = owners(directory)
                self.assertEqual(orders.snapshots[-1].state, "PARTIALLY_FILLED")
                self.assertEqual(economic.position(session.INSTRUMENT), Decimal("1"))
                status = get_status(directory)
                self.assertEqual(status["recovery_disposition"], "RETAINED_FILL_RECOVERY")
                with patch.object(SimulatedProvider, "transport_send", side_effect=AssertionError("no resend")), patch.object(
                        session.AuthorityService, "admit", side_effect=AssertionError("no new admission")):
                    recovered = run(directory, PRICES, stop_after_episodes=episode, execution_profile=PROFILE)
                self.assertEqual(recovered["decisions"], expected["decisions"])
                self.assertEqual(recovered["new_outbound_requests"], 0)
                before = store.whole_store_state_cut()
                self.assertEqual(run(directory, PRICES, stop_after_episodes=episode, execution_profile=PROFILE)["new_outbound_requests"], 0)
                self.assertEqual(store.whole_store_state_cut(), before)
                self.assertEqual(len(owners(directory)[4].obligations), 2 * (episode - 2))

    def test_frozen_partial_profile_pause_resume_and_cli_do_not_duplicate_fills(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as reference:
            expected = run(reference, PRICES, execution_profile=PROFILE)
            run(directory, PRICES, stop_after_episodes=4, execution_profile=PROFILE)
            with self.assertRaisesRegex(ValueError, "protocol/input identity changed"):
                run(directory, PRICES)
            actual = run(directory, PRICES, execution_profile=PROFILE)
            self.assertEqual(actual["decisions"], expected["decisions"])
            self.assertEqual(actual["cash"], expected["cash"])
            with patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main(["--state-dir", directory, "--autonomous-simulation", "--episode-id", "acceptance",
                    "--at", NOW, "--prices", ",".join(PRICES), "--execution-profile", PROFILE, "--target-quantity", "2"]), 0)
            self.assertEqual(len(owners(directory)[1].transactions), 7)
            self.assertTrue(get_economic_report(directory)["financial_equality_verified"])
            store = owners(directory)[0]
            seed = store.load_events_by_aggregate_type("economic_book")[0]
            first_checkpoint = store.load_events_by_aggregate_type("account_reconciliation")[0]
            self.assertEqual(seed["committed_at"], NOW)
            self.assertLess(datetime.fromisoformat(seed["committed_at"]),
                            datetime.fromisoformat(first_checkpoint["payload"]["resource_availability"]["query_started_at"]))

    def test_partial_finances_equal_full_fill_and_ignore_ambient_decimal_precision(self):
        with TemporaryDirectory() as partial, TemporaryDirectory() as full:
            expected = run(full, PRICES)
            with localcontext() as context:
                context.prec = 2
                context.traps[Inexact] = context.traps[Rounded] = True
                actual = run(partial, PRICES, execution_profile=PROFILE)
            self.assertEqual((actual["cash"], actual["position"]), (expected["cash"], expected["position"]))
            partial_report = get_economic_report(partial)
            full_report = get_economic_report(full)
            self.assertNotEqual(partial_report["journal_sequence"], full_report["journal_sequence"])
            self.assertEqual(
                {key: value for key, value in partial_report.items() if key != "journal_sequence"},
                {key: value for key, value in full_report.items() if key != "journal_sequence"},
            )

    def test_policy_registration_uses_frozen_time_and_matches_after_restart(self):
        original_register = session.AuthorityService.register_policy

        def guarded_register(service, policy, **kwargs):
            if service.store is not None:
                with patch.object(authority_module, "datetime", _NoWallClock):
                    return original_register(service, policy, **kwargs)
            # An in-memory validation probe emits no durable policy event.
            return original_register(service, policy, **kwargs)

        for profile in ("IMMEDIATE", PROFILE):
            with self.subTest(profile=profile), TemporaryDirectory() as full, TemporaryDirectory() as resumed:
                with patch.object(session.AuthorityService, "register_policy", guarded_register):
                    run(full, PRICES, execution_profile=profile)
                    run(resumed, PRICES, stop_after_episodes=4, execution_profile=profile)
                    run(resumed, PRICES, execution_profile=profile)
                def registrations(directory):
                    return [(event["event_id"], event["committed_at"], event["payload_hash"])
                            for event in owners(directory)[0].load_events_by_aggregate_type("authority_state")
                            if event["event_type"] == "AuthorityPolicyRegistered"]
                expected = registrations(full)
                self.assertEqual(expected, registrations(resumed))
                self.assertEqual([timestamp for _, timestamp, _ in expected], [
                    "2026-10-03T00:00:02.000001Z", "2026-10-03T00:00:03.000001Z",
                    "2026-10-03T00:00:05.000001Z",
                ])

    def test_invalid_frozen_quantity_or_profile_is_rejected_before_state_creation(self):
        for profile, quantity in ((PROFILE, "1"), (PROFILE, "3"), ("UNKNOWN", "2"),
                                  (PROFILE, "0"), (PROFILE, "NaN"), (PROFILE, 2.0)):
            with self.subTest(profile=profile, quantity=quantity), TemporaryDirectory() as root:
                state = Path(root) / "must-not-exist"
                with self.assertRaises((ValueError, TypeError)):
                    base_run(state, PRICES, execution_profile=profile, target_quantity=quantity)
                self.assertFalse(state.exists())

    def test_missing_reordered_or_duplicate_retained_partial_fails_without_mutation(self):
        with TemporaryDirectory() as directory:
            with patch.object(session, "commit_order_fill_with_reservation_consumption", side_effect=RuntimeError("crash")):
                with self.assertRaisesRegex(RuntimeError, "crash"):
                    run(directory, PRICES, execution_profile=PROFILE)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            before = store.whole_store_state_cut()
            original = JournalStore.load_events
            for kind in ("missing", "reordered", "duplicate"):
                def changed(selected, aggregate_type, aggregate_id):
                    events = original(selected, aggregate_type, aggregate_id)
                    if aggregate_type == "canonical_autonomous_simulation":
                        events = deepcopy(events)
                        payload = events[-1]["payload"]
                        fills = payload["fills"]
                        payload["fills"] = fills[:1] if kind == "missing" else fills[::-1] if kind == "reordered" else [fills[0], fills[0]]
                    return events
                with self.subTest(kind=kind), patch.object(JournalStore, "load_events", changed), patch.object(
                        SimulatedProvider, "transport_send", side_effect=AssertionError("no resend")):
                    with self.assertRaisesRegex(ValueError, "retained fill observation|retained simulator history"):
                        run(directory, PRICES, execution_profile=PROFILE)
                self.assertEqual(store.whole_store_state_cut(), before)


if __name__ == "__main__":
    unittest.main()
