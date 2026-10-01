from __future__ import annotations

from types import SimpleNamespace
from threading import Event, Thread
import unittest

from mvp.autotrade_mvp.host_network import TransportResponse
from mvp.autotrade_mvp.production_host import (
    ProductionHostRuntime,
    _CommandAdmissionGate,
)


class ProductionHostLifecycleCurrentTests(unittest.TestCase):
    def test_shutdown_cut_rejects_new_commands_and_waits_for_admitted_dispatch(self):
        entered = Event()
        release = Event()
        drained = Event()
        results: list[TransportResponse] = []

        class Application:
            def dispatch(self, **kwargs):
                del kwargs
                entered.set()
                release.wait(timeout=5)
                return TransportResponse(200, "application/json", b"{}")

        gate = _CommandAdmissionGate(Application())
        admitted = Thread(
            target=lambda: results.append(
                gate.dispatch(
                    method="POST",
                    target="/api/v1/commands",
                    headers={},
                    body=b"{}",
                )
            ),
            daemon=False,
        )
        admitted.start()
        self.assertTrue(entered.wait(timeout=2))

        closer = Thread(
            target=lambda: (gate.stop_and_drain(), drained.set()),
            daemon=False,
        )
        closer.start()

        rejected = gate.dispatch(
            method="POST",
            target="/api/v1/commands",
            headers={},
            body=b"{}",
        )
        self.assertEqual(rejected.status, 503)
        self.assertEqual(rejected.body, b'{"error":"HOST_SHUTTING_DOWN"}')
        self.assertFalse(drained.wait(timeout=0.05))

        release.set()
        self.assertTrue(drained.wait(timeout=2))
        admitted.join(timeout=2)
        closer.join(timeout=2)
        self.assertFalse(admitted.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, 200)

    def test_terminal_teardown_releases_process_fence_only_after_drain_and_listener_close(self):
        order: list[str] = []

        class Gate:
            def stop_accepting(self):
                order.append("stop_accepting")

            def stop_and_drain(self):
                order.append("stop_and_drain")

        class Server:
            def server_close(self):
                order.append("server_close")

        class Fence:
            def release(self):
                order.append("fence_release")

        runtime = ProductionHostRuntime(
            config=SimpleNamespace(),
            journal=SimpleNamespace(store_identity="store-generation"),
            application=SimpleNamespace(),
            server=Server(),
            instance_fence=Fence(),
            admission_gate=Gate(),
        )
        runtime.close()
        self.assertTrue(runtime.closed)
        self.assertEqual(
            order,
            [
                "stop_accepting",
                "stop_and_drain",
                "server_close",
                "fence_release",
            ],
        )


if __name__ == "__main__":
    unittest.main()
