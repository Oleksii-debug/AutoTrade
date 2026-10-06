import unittest
from io import BytesIO

from mvp.autotrade_mvp.provider_transport import (
    ProviderTransportScopeError,
    SignedHttpRequest,
    TradingWireResponse,
    UrllibJsonWireClient,
)


class SignedHttpRequestEnvelopeTests(unittest.TestCase):
    def test_exact_write_envelope_preserves_admitted_url_and_post_method(self):
        request = SignedHttpRequest(
            method="post",
            url="https://api.example.test/v1/order",
            headers={"Content-Type": "application/json"},
            body=b"{}",
            timeout_seconds=5,
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url, "https://api.example.test/v1/order")
        self.assertEqual(dict(request.headers), {"Content-Type": "application/json"})

    def test_write_url_and_method_reject_executable_or_noncanonical_text(self):
        class HostileText(str):
            def strip(self):
                raise AssertionError("hostile strip executed")

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "signed request URL is invalid",
        ):
            SignedHttpRequest(
                method="POST",
                url=" https://api.example.test/v1/order ",
                headers={"Content-Type": "application/json"},
                body=b"{}",
                timeout_seconds=5,
            )

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "signed request URL is invalid",
        ):
            SignedHttpRequest(
                method="POST",
                url=HostileText("https://api.example.test/v1/order"),
                headers={"Content-Type": "application/json"},
                body=b"{}",
                timeout_seconds=5,
            )

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "signed request method must be canonical text",
        ):
            SignedHttpRequest(
                method=HostileText("POST"),
                url="https://api.example.test/v1/order",
                headers={"Content-Type": "application/json"},
                body=b"{}",
                timeout_seconds=5,
            )

    def test_write_url_rejects_parser_normalized_or_non_ascii_octets(self):
        hostile_urls = (
            "https://exa\nmple.com/v1/order",
            "https://exa\rmple.com/v1/order",
            "https://exa\tmple.com/v1/order",
            "https://api.example.test/v1/or\x00der",
            "https://api.example.test/v1/or\x1fder",
            "https://api.example.test/v1/or\x7fder",
            "https://api.example.test/v1/or\u0085der",
            "https://api.example.test/v1\\order",
            "https://api.example.test/v1 /order",
        )
        for url in hostile_urls:
            with self.subTest(url=repr(url)):
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    "signed request URL is invalid",
                ):
                    SignedHttpRequest(
                        method="POST",
                        url=url,
                        headers={"Content-Type": "application/json"},
                        body=b"{}",
                        timeout_seconds=5,
                    )

        percent_encoded = SignedHttpRequest(
            method="POST",
            url="https://api.example.test/v1/order?symbol=%E2%82%AC",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=b"",
            timeout_seconds=5,
        )
        self.assertEqual(
            percent_encoded.url,
            "https://api.example.test/v1/order?symbol=%E2%82%AC",
        )

    def test_write_headers_and_timeout_fail_before_virtual_callbacks(self):
        class HostileHeaders(dict):
            def items(self):
                raise AssertionError("hostile items executed")

        class HostileInt(int):
            def __lt__(self, other):
                raise AssertionError("hostile less-than executed")

            def __gt__(self, other):
                raise AssertionError("hostile greater-than executed")

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "signed request headers must be an exact inert mapping",
        ):
            SignedHttpRequest(
                method="POST",
                url="https://api.example.test/v1/order",
                headers=HostileHeaders({"Content-Type": "application/json"}),
                body=b"{}",
                timeout_seconds=5,
            )

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "signed request header values must be canonical text",
        ):
            SignedHttpRequest(
                method="POST",
                url="https://api.example.test/v1/order",
                headers={"Content-Type": " application/json "},
                body=b"{}",
                timeout_seconds=5,
            )

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "invalid request timeout",
        ):
            SignedHttpRequest(
                method="POST",
                url="https://api.example.test/v1/order",
                headers={"Content-Type": "application/json"},
                body=b"{}",
                timeout_seconds=HostileInt(5),
            )

    def test_write_headers_reject_wire_grammar_drift_and_case_duplicates(self):
        invalid_names = (
            "X:Injected",
            "X-\x00Injected",
            "X-É",
        )
        for name in invalid_names:
            with self.subTest(name=repr(name)):
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    "signed request header names must be canonical text",
                ):
                    SignedHttpRequest(
                        method="POST",
                        url="https://api.example.test/v1/order",
                        headers={name: "value"},
                        body=b"{}",
                        timeout_seconds=5,
                    )

        invalid_values = (
            "value\x00tail",
            "value\x1ftail",
            "value\x7ftail",
            "value\u0085tail",
        )
        for value in invalid_values:
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(
                    ProviderTransportScopeError,
                    "signed request header values must be canonical text",
                ):
                    SignedHttpRequest(
                        method="POST",
                        url="https://api.example.test/v1/order",
                        headers={"X-Test": value},
                        body=b"{}",
                        timeout_seconds=5,
                    )

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "signed request header names must be unique case-insensitively",
        ):
            SignedHttpRequest(
                method="POST",
                url="https://api.example.test/v1/order",
                headers={
                    "Authorization": "Bearer good",
                    "authorization": "Bearer evil",
                },
                body=b"{}",
                timeout_seconds=5,
            )

        canonical = SignedHttpRequest(
            method="POST",
            url="https://api.example.test/v1/order",
            headers={
                "Authorization": "Bearer abc+/_-.=~",
                "X-Trace_Id": "visible ASCII value",
            },
            body=b"{}",
            timeout_seconds=5,
        )
        self.assertEqual(
            dict(canonical.headers)["Authorization"],
            "Bearer abc+/_-.=~",
        )

    def test_wire_rejects_signed_request_subclass_before_field_access(self):
        class ExecutableSignedRequest(SignedHttpRequest):
            callbacks = 0

            def __getattribute__(self, name):
                if name in {"url", "headers", "timeout_seconds", "method", "body"}:
                    type(self).callbacks += 1
                    raise AssertionError("signed request subclass field access executed")
                return super().__getattribute__(name)

        forged = object.__new__(ExecutableSignedRequest)
        client = UrllibJsonWireClient(max_response_bytes=1024)

        with self.assertRaisesRegex(
            TypeError,
            "exact SignedHttpRequest or exact AuthenticatedReadHttpRequest",
        ):
            client.send(forged)

        self.assertEqual(ExecutableSignedRequest.callbacks, 0)

    def test_exact_signed_request_still_reaches_wire_as_post(self):
        class Response:
            status = 200

            def __init__(self):
                self.body = BytesIO(b'{"accepted":true}')

            def read(self, size=-1):
                return self.body.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, *, timeout):
                self.requests.append((request, timeout))
                return Response()

        request = SignedHttpRequest(
            method="POST",
            url="https://api.example.test/v1/order",
            headers={"Content-Type": "application/json"},
            body=b"{}",
            timeout_seconds=5,
        )
        client = UrllibJsonWireClient(max_response_bytes=1024)
        opener = Opener()
        client._opener = opener

        response = client.send(request)

        self.assertIs(type(response), TradingWireResponse)
        self.assertEqual(response.http_status, 200)
        self.assertEqual(response.body, b'{"accepted":true}')
        self.assertEqual(len(opener.requests), 1)
        outbound, timeout = opener.requests[0]
        self.assertEqual(outbound.get_method(), "POST")
        self.assertEqual(outbound.data, b"{}")
        self.assertEqual(timeout, 5)


    def test_wire_preserves_canonical_signed_header_values(self):
        class Response:
            status = 200

            def __init__(self):
                self.body = BytesIO(b'{"accepted":true}')

            def read(self, size=-1):
                return self.body.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, *, timeout):
                self.requests.append((request, timeout))
                return Response()

        request = SignedHttpRequest(
            method="POST",
            url="https://api.example.test/v1/order",
            headers={
                "Authorization": "Bearer abc+/_-.=~",
                "X-BAPI-SIGN": "ABCDEF0123456789",
            },
            body=b"{}",
            timeout_seconds=5,
        )
        client = UrllibJsonWireClient(max_response_bytes=1024)
        opener = Opener()
        client._opener = opener

        response = client.send(request)

        self.assertIs(type(response), TradingWireResponse)
        outbound, timeout = opener.requests[0]
        actual_headers = {
            name.lower(): value
            for name, value in outbound.header_items()
        }
        self.assertEqual(
            actual_headers["authorization"],
            "Bearer abc+/_-.=~",
        )
        self.assertEqual(
            actual_headers["x-bapi-sign"],
            "ABCDEF0123456789",
        )
        self.assertEqual(timeout, 5)

    def test_wire_rejects_post_construction_signed_request_mutation_zero_wire(self):
        class Opener:
            def __init__(self):
                self.calls = 0

            def open(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("wire must not be reached")

        request = SignedHttpRequest(
            method="POST",
            url="https://api.example.test/v1/order",
            headers={"Content-Type": "application/json"},
            body=b"{}",
            timeout_seconds=5,
        )
        object.__setattr__(
            request,
            "url",
            "https://attacker.invalid/v1/order",
        )
        client = UrllibJsonWireClient(max_response_bytes=1024)
        opener = Opener()
        client._opener = opener

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "signed request changed after construction",
        ):
            client.send(request)

        self.assertEqual(opener.calls, 0)

    def test_wire_rejects_unissued_exact_signed_request_zero_wire(self):
        class Opener:
            def __init__(self):
                self.calls = 0

            def open(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("wire must not be reached")

        forged = object.__new__(SignedHttpRequest)
        object.__setattr__(forged, "method", "POST")
        object.__setattr__(forged, "url", "https://api.example.test/v1/order")
        object.__setattr__(
            forged,
            "headers",
            {"Content-Type": "application/json"},
        )
        object.__setattr__(forged, "body", b"{}")
        object.__setattr__(forged, "timeout_seconds", 5)

        client = UrllibJsonWireClient(max_response_bytes=1024)
        opener = Opener()
        client._opener = opener

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "signed request lacks construction authority",
        ):
            client.send(forged)

        self.assertEqual(opener.calls, 0)


if __name__ == "__main__":
    unittest.main()
