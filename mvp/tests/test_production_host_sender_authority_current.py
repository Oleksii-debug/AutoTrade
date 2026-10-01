from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest

from mvp.autotrade_mvp.host_network import TransportResponse
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_host import (
    ProductionHostRuntime,
    _CommandAdmissionGate,
)
from mvp.autotrade_mvp.recovery import RecoveryController


class _Application:
    def __init__(self, dispatch):
        self.dispatch = dispatch


class _Server:
    def __init__(self, ordering):
        self.ordering = ordering

    def server_close(self):
        self.ordering.append("listener-closed")


class _Fence:
    def __init__(self, ordering):
        self.ordering = ordering

    def release(self):
        self.ordering.append("fence-released")


class ProductionHostCurrentSenderAuthorityTests(unittest.TestCase):
    def test_recovery_issued_dispatcher_ignores_hostile_legacy_sender_callback(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            recovery = RecoveryController(
                owner_store=journal,
                owner_scope="PAPER:acct",
            )
            recovery.start("host-a")
            dispatcher = recovery.build_guarded_dispatcher(
                journal,
                environment="PAPER",
                account_id="acct",
            )
            hostile_sender_check_calls = []
            wire_calls = []

            def hostile_sender_check(_owner_id, _owner_epoch):
                hostile_sender_check_calls.append("called")
                raise AssertionError("legacy sender callback must not replace issued authority")

            def transport_send(_client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                raise AssertionError("wire must remain blocked before recovery is READY")

            outcome = dispatcher.dispatch(
                attempt_id="attempt-1",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                provider="KRAKEN",
                request={"pair": "XBTUSD"},
                now="2026-10-01T00:00:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport_send,
                sender_check=hostile_sender_check,
            )
            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(hostile_sender_check_calls, [])
            self.assertEqual(wire_calls, [])

    def test_shutdown_cut_drains_admitted_command_before_listener_and_fence_release(self):
        entered = Event()
        release = Event()
        ordering = []

        def dispatch(**_kwargs):
            entered.set()
            release.wait(timeout=2)
            ordering.append("command-finished")
            return TransportResponse(
                status=200,
                content_type="application/json; charset=utf-8",
                body=b"{}",
                headers=(),
            )

        gate = _CommandAdmissionGate(_Application(dispatch))
        response = []
        worker = Thread(
            target=lambda: response.append(
                gate.dispatch(
                    method="POST",
                    target="/api/v1/commands",
                    headers={},
                    body=b"{}",
                )
            )
        )
        worker.start()
        self.assertTrue(entered.wait(timeout=1))

        class JournalStub:
            store_identity = object()

        runtime = ProductionHostRuntime(
            config=object(),
            journal=JournalStub(),
            application=object(),
            server=_Server(ordering),
            instance_fence=_Fence(ordering),
            admission_gate=gate,
            recovery_controller=object(),
            financial_dispatcher=object(),
        )
        closed = Event()
        closer = Thread(target=lambda: (runtime.close(), closed.set()))
        closer.start()
        self.assertFalse(closed.wait(timeout=0.05))

        rejected = gate.dispatch(
            method="POST",
            target="/api/v1/commands",
            headers={},
            body=b"{}",
        )
        self.assertEqual(rejected.status, 503)

        release.set()
        worker.join(timeout=2)
        closer.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(response[0].status, 200)
        self.assertEqual(
            ordering,
            ["command-finished", "listener-closed", "fence-released"],
        )


if __name__ == "__main__":
    unittest.main()
