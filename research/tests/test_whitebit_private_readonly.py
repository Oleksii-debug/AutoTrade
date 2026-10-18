"""Offline WhiteBIT private READ tests; no account or network calls."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from autotrade_research.integrations.whitebit_private_readonly import (
    MAIN_BALANCE_PATH,
    MAX_RESPONSE_BYTES,
    NETWORK_TIMEOUT_SECONDS,
    PrivateHttpResponse,
    PrivateReadError,
    StdlibWhitebitHttpsTransport,
    TRADING_BALANCE_PATH,
    WHITEBIT_PRIVATE_HOST,
    WHITEBIT_PRIVATE_PORT,
    WhitebitPrivateReader,
    _validate_request,
)


class RecordingTransport:
    def __init__(self, responder=None):
        self.calls = []
        self.closed = 0
        self.responder = responder

    def post(self, **kwargs):
        self.calls.append(kwargs)
        if self.responder is not None:
            return self.responder(kwargs)
        return PrivateHttpResponse(200, "application/json", b"{}")

    def close(self):
        self.closed += 1


class WhitebitPrivateReadTests(unittest.TestCase):
    def test_signature_matches_exact_wire_body(self):
        transport = RecordingTransport()
        with WhitebitPrivateReader(
            "fixture-key-only", "fixture-signing-only", transport=transport
        ) as reader:
            result = reader.main_balance()
        call = transport.calls[0]
        headers = call["headers"]
        self.assertEqual(base64.b64decode(headers["X-TXC-PAYLOAD"]), call["body"])
        expected = hmac.new(
            b"fixture-signing-only",
            headers["X-TXC-PAYLOAD"].encode("ascii"),
            hashlib.sha512,
        ).hexdigest()
        self.assertEqual(headers["X-TXC-SIGNATURE"], expected)
        self.assertEqual(headers["X-TXC-APIKEY"], "fixture-key-only")
        self.assertEqual(json.loads(call["body"])["request"], MAIN_BALANCE_PATH)
        self.assertEqual(call["timeout_seconds"], NETWORK_TIMEOUT_SECONDS)
        self.assertEqual(call["maximum_response_bytes"], MAX_RESPONSE_BYTES)
        self.assertFalse(result.verified_account_binding)
        self.assertEqual(result.trading_authority, "NONE")
        self.assertNotIn("fixture", repr(result))
        self.assertEqual(hashlib.sha256(result.body).hexdigest(), result.raw_sha256)

    def test_default_network_is_disabled(self):
        with self.assertRaisesRegex(PrivateReadError, "NOT_ENABLED"):
            WhitebitPrivateReader("fixture-key-only", "fixture-signing-only")

    def test_test_transport_cannot_be_misrepresented_as_network_binding(self):
        with self.assertRaisesRegex(PrivateReadError, "AUTHORITY_AMBIGUOUS"):
            WhitebitPrivateReader(
                "key", "secret", transport=RecordingTransport(), enable_network=True
            )

    def test_explicit_network_path_is_marked_bound_only_after_successful_read(self):
        transport = RecordingTransport()
        with patch(
            "autotrade_research.integrations.whitebit_private_readonly."
            "StdlibWhitebitHttpsTransport",
            return_value=transport,
        ):
            with WhitebitPrivateReader("key", "secret", enable_network=True) as reader:
                observation = reader.main_balance()
        self.assertTrue(observation.verified_account_binding)
        self.assertEqual(observation.trading_authority, "NONE")

    def test_nonce_is_globally_monotonic_when_clock_does_not_move(self):
        transport = RecordingTransport()
        with patch(
            "autotrade_research.integrations.whitebit_private_readonly.time.time_ns",
            return_value=1_000_000_000_000_000,
        ):
            with WhitebitPrivateReader("key-one", "secret-one", transport=transport) as one:
                one.main_balance()
            with WhitebitPrivateReader("key-two", "secret-two", transport=transport) as two:
                two.main_balance()
        nonces = [json.loads(call["body"])["nonce"] for call in transport.calls]
        self.assertLess(nonces[0], nonces[1])

    def test_only_two_exact_balance_routes_exist(self):
        transport = RecordingTransport()
        with WhitebitPrivateReader("key", "secret", transport=transport) as reader:
            wallet = reader.main_balance()
            spot = reader.trading_balance()
        self.assertEqual(
            [call["path"] for call in transport.calls],
            [MAIN_BALANCE_PATH, TRADING_BALANCE_PATH],
        )
        self.assertEqual(wallet.path, MAIN_BALANCE_PATH)
        self.assertEqual(spot.path, TRADING_BALANCE_PATH)
        self.assertEqual(spot.trading_authority, "NONE")
        self.assertFalse(
            any(
                token in (MAIN_BALANCE_PATH + TRADING_BALANCE_PATH).lower()
                for token in ("order", "withdraw", "transfer", "leverage")
            )
        )

    def test_foreign_or_state_changing_routes_fail_before_io(self):
        body = json.dumps(
            {"request": MAIN_BALANCE_PATH, "nonce": 1, "nonceWindow": True},
            separators=(",", ":"),
        ).encode()
        payload = base64.b64encode(body).decode()
        headers = {
            "Content-Type": "application/json",
            "X-TXC-APIKEY": "key",
            "X-TXC-PAYLOAD": payload,
            "X-TXC-SIGNATURE": "signature",
        }
        for path in (
            "/api/v4/order/new",
            "/api/v4/main-account/withdraw",
            "/api/v4/main-account/transfer",
            MAIN_BALANCE_PATH + "/extra",
            MAIN_BALANCE_PATH + "?x=1",
        ):
            with self.subTest(path=path), self.assertRaisesRegex(
                PrivateReadError, "ROUTE_DENIED"
            ):
                _validate_request(path, headers, body)

    def test_cross_signed_and_extended_payloads_are_denied(self):
        for path, document in (
            (
                TRADING_BALANCE_PATH,
                {"request": MAIN_BALANCE_PATH, "nonce": 1, "nonceWindow": True},
            ),
            (
                MAIN_BALANCE_PATH,
                {
                    "request": MAIN_BALANCE_PATH,
                    "nonce": 1,
                    "nonceWindow": True,
                    "ticker": "BTC",
                },
            ),
        ):
            body = json.dumps(document, separators=(",", ":")).encode()
            headers = {
                "Content-Type": "application/json",
                "X-TXC-APIKEY": "key",
                "X-TXC-PAYLOAD": base64.b64encode(body).decode(),
                "X-TXC-SIGNATURE": "signature",
            }
            with self.subTest(path=path), self.assertRaisesRegex(
                PrivateReadError, "REQUEST_INVALID"
            ):
                _validate_request(path, headers, body)

    def test_http_errors_redirects_and_remote_body_are_not_echoed(self):
        for code in (302, 401, 429, 500):
            transport = RecordingTransport(
                lambda _: PrivateHttpResponse(
                    code, "application/json", b'{"secret":"private-do-not-echo"}'
                )
            )
            with self.subTest(code=code), WhitebitPrivateReader(
                "key", "secret", transport=transport
            ) as reader:
                with self.assertRaises(PrivateReadError) as caught:
                    reader.main_balance()
                self.assertNotIn("private-do-not-echo", str(caught.exception))

    def test_injected_exception_is_secret_free_and_unchained(self):
        def fail(_):
            raise OSError("secret-one key-one")

        with WhitebitPrivateReader(
            "key-one", "secret-one", transport=RecordingTransport(fail)
        ) as reader:
            with self.assertRaisesRegex(PrivateReadError, "IO_FAILED") as caught:
                reader.main_balance()
        self.assertNotIn("secret-one", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_response_contract_is_bounded_json_object(self):
        cases = (
            (PrivateHttpResponse(200, "text/html", b"{}"), "MEDIA_INVALID"),
            (PrivateHttpResponse(200, "application/json", b""), "EMPTY_RESPONSE"),
            (
                PrivateHttpResponse(
                    200, "application/json", b"x" * (MAX_RESPONSE_BYTES + 1)
                ),
                "TOO_LARGE",
            ),
            (PrivateHttpResponse(200, "application/json", b"not-json"), "JSON_INVALID"),
            (PrivateHttpResponse(200, "application/json", b"[]"), "JSON_INVALID"),
            (
                PrivateHttpResponse(200, "application/json", b'{"USDT":{},"USDT":{}}'),
                "JSON_INVALID",
            ),
            (
                PrivateHttpResponse(200, "application/json", b'{"USDT":NaN}'),
                "JSON_INVALID",
            ),
            (
                PrivateHttpResponse(
                    200,
                    "application/json",
                    b'{"code":1,"message":"private-do-not-echo"}',
                ),
                "REMOTE_REJECTED",
            ),
            (PrivateHttpResponse("private", "application/json", b"{}"), "RESPONSE_INVALID"),
        )
        for response, message in cases:
            with self.subTest(message=message), WhitebitPrivateReader(
                "key", "secret", transport=RecordingTransport(lambda _: response)
            ) as reader:
                with self.assertRaisesRegex(PrivateReadError, message):
                    reader.main_balance()

    def test_closed_session_and_transport_cleanup(self):
        transport = RecordingTransport()
        reader = WhitebitPrivateReader("key", "secret", transport=transport)
        reader.close()
        reader.close()
        self.assertEqual(transport.closed, 1)
        with self.assertRaisesRegex(PrivateReadError, "CLOSED"):
            reader.main_balance()

    def test_credential_header_injection_is_rejected(self):
        for value in ("", "x\ny", "x y", 1, True):
            with self.subTest(value=value), self.assertRaisesRegex(
                PrivateReadError, "CREDENTIAL_INPUT_INVALID"
            ):
                WhitebitPrivateReader(value, "secret", transport=RecordingTransport())

    def test_stdlib_transport_uses_fixed_direct_tls_authority_and_path(self):
        events = []

        class Response:
            status = 200

            @staticmethod
            def getheader(name, default=None):
                return {"Content-Type": "application/json"}.get(name, default)

            @staticmethod
            def read(limit):
                events.append(("read", limit))
                return b"{}"

        class Connection:
            def __init__(self, host, port, *, timeout, context):
                events.append(("connect", host, port, timeout, context))

            def request(self, method, path, *, body, headers):
                events.append(("request", method, path, body, headers))

            @staticmethod
            def getresponse():
                return Response()

            def close(self):
                events.append(("close",))

        recorder = RecordingTransport()
        with WhitebitPrivateReader("key", "secret", transport=recorder) as reader:
            reader.main_balance()
        call = recorder.calls[0]
        context = object()
        transport = StdlibWhitebitHttpsTransport()
        with (
            patch(
                "autotrade_research.integrations.whitebit_private_readonly."
                "ssl.create_default_context",
                return_value=context,
            ),
            patch(
                "autotrade_research.integrations.whitebit_private_readonly."
                "http.client.HTTPSConnection",
                Connection,
            ),
        ):
            response = transport.post(**call)
        self.assertEqual(response.body, b"{}")
        self.assertEqual(
            events[0],
            ("connect", WHITEBIT_PRIVATE_HOST, WHITEBIT_PRIVATE_PORT, 10.0, context),
        )
        self.assertEqual(events[1][1:3], ("POST", MAIN_BALANCE_PATH))
        self.assertEqual(events[-1], ("close",))

    def test_imports_under_isolated_python_without_site_packages(self):
        root = Path(__file__).resolve().parents[2]
        script = (
            "import sys;"
            + f"sys.path.insert(0,{str(root / 'research')!r});"
            + "from autotrade_research.integrations.whitebit_private_readonly "
            + "import WhitebitPrivateReader;"
            + "print(WhitebitPrivateReader.__name__)"
        )
        completed = subprocess.run(
            [sys.executable, "-S", "-c", script],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "WhitebitPrivateReader")


if __name__ == "__main__":
    unittest.main()
