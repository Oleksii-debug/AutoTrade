"""Deterministic shutdown/readiness cut regressions for the production host."""

from threading import Event, Thread
import unittest
from unittest.mock import Mock

from mvp.autotrade_mvp.host_network import TransportResponse
from mvp.autotrade_mvp.production_host import _CommandAdmissionGate


_READY = TransportResponse(
    status=200,
    content_type="application/json; charset=utf-8",
    body=(
        b'{"affected_scope":["HOST_API"],"as_of":"2026-09-30T02:00:00Z",'
        b'"component":"HOST_NETWORK","reason_codes":[],"status":"READY"}'
    ),
    headers=(("Cache-Control", "no-store"),),
)


class ProductionHostReadinessCutTests(unittest.TestCase):
    def test_health_after_shutdown_cut_cannot_report_ready(self):
        application = Mock()
        application.dispatch.return_value = _READY
        gate = _CommandAdmissionGate(application)

        gate.stop_accepting()
        response = gate.dispatch(
            method="GET",
            target="/api/v1/health",
            headers={},
        )

        self.assertEqual(response.status, 503)
        self.assertEqual(response.body, b'{"error":"HOST_SHUTTING_DOWN"}')
        self.assertNotIn(b'"status":"READY"', response.body)

    def test_health_finishing_after_shutdown_cut_cannot_report_ready(self):
        entered = Event()
        release = Event()

        def dispatch(**kwargs):
            del kwargs
            entered.set()
            release.wait()
            return _READY

        application = Mock()
        application.dispatch = dispatch
        gate = _CommandAdmissionGate(application)
        responses = []
        request = Thread(
            target=lambda: responses.append(
                gate.dispatch(
                    method="GET",
                    target="/api/v1/health",
                    headers={},
                )
            )
        )
        request.start()
        self.assertTrue(entered.wait(timeout=1))

        gate.stop_accepting()
        release.set()
        request.join(timeout=1)

        self.assertFalse(request.is_alive())
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].status, 503)
        self.assertEqual(responses[0].body, b'{"error":"HOST_SHUTTING_DOWN"}')
        self.assertNotIn(b'"status":"READY"', responses[0].body)

    def test_invalid_health_request_keeps_underlying_validation_error(self):
        invalid = TransportResponse(
            status=400,
            content_type="application/json; charset=utf-8",
            body=b'{"error":"INVALID_QUERY"}',
        )
        application = Mock()
        application.dispatch.return_value = invalid
        gate = _CommandAdmissionGate(application)
        gate.stop_accepting()

        response = gate.dispatch(
            method="GET",
            target="/api/v1/health?unexpected=1",
            headers={},
        )

        self.assertIs(response, invalid)


if __name__ == "__main__":
    unittest.main()
