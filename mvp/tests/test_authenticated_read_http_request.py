import unittest
from io import BytesIO

from mvp.autotrade_mvp.provider_transport import (
    AuthenticatedReadHttpRequest,
    AuthenticatedReadWireResponse,
    ProviderTransportScopeError,
    UrllibJsonWireClient,
)


class AuthenticatedReadHttpRequestTests(unittest.TestCase):
    def test_empty_body_post_without_query_is_valid_read_envelope(self):
        request = AuthenticatedReadHttpRequest(
            url="https://localhost/iserver/auth/status",
            headers={"Accept": "application/json"},
            timeout_seconds=5,
            method="POST",
            body=b"",
        )

        self.assertEqual(request.method, "POST")
        self.assertEqual(request.body, b"")
        self.assertEqual(request.url, "https://localhost/iserver/auth/status")

    def test_post_still_rejects_url_query_even_when_body_is_empty(self):
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "authenticated POST requires no URL query",
        ):
            AuthenticatedReadHttpRequest(
                url="https://localhost/iserver/auth/status?unexpected=1",
                headers={"Accept": "application/json"},
                timeout_seconds=5,
                method="POST",
                body=b"",
            )

    def test_get_allows_queryless_session_read_but_still_rejects_body(self):
        queryless = AuthenticatedReadHttpRequest(
            url="https://localhost/iserver/accounts",
            headers={"Accept": "application/json"},
            timeout_seconds=5,
            method="GET",
            body=b"",
        )
        self.assertEqual(queryless.method, "GET")
        self.assertEqual(queryless.body, b"")
        self.assertEqual(queryless.url, "https://localhost/iserver/accounts")

        signed = AuthenticatedReadHttpRequest(
            url="https://api.example.test/read?signature=synthetic",
            headers={"X-API-KEY": "synthetic"},
            timeout_seconds=5,
            method="GET",
            body=b"",
        )
        self.assertEqual(signed.method, "GET")

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "authenticated GET requires no body",
        ):
            AuthenticatedReadHttpRequest(
                url="https://api.example.test/read?signature=synthetic",
                headers={"X-API-KEY": "synthetic"},
                timeout_seconds=5,
                method="GET",
                body=b"payload",
            )

    def test_nonempty_post_contract_remains_valid_for_body_signed_reads(self):
        request = AuthenticatedReadHttpRequest(
            url="https://api.kraken.com/0/private/OpenOrders",
            headers={"API-Key": "synthetic"},
            timeout_seconds=5,
            method="POST",
            body=b"nonce=1",
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.body, b"nonce=1")

    def test_wire_client_preserves_explicit_empty_body_post_method(self):
        class Response:
            status = 200

            def __init__(self):
                self.body = BytesIO(b'{"success":{"value":{"connected":true}}}')

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

        request = AuthenticatedReadHttpRequest(
            url="https://localhost/iserver/auth/status",
            headers={"Accept": "application/json"},
            timeout_seconds=5,
            method="POST",
            body=b"",
        )
        client = UrllibJsonWireClient(max_response_bytes=1024)
        opener = Opener()
        client._opener = opener

        response = client.send(request)

        self.assertIs(type(response), AuthenticatedReadWireResponse)
        self.assertEqual(response.http_status, 200)
        self.assertEqual(
            response.body,
            b'{"success":{"value":{"connected":true}}',
        )
        self.assertEqual(len(opener.requests), 1)
        outbound, timeout = opener.requests[0]
        self.assertEqual(outbound.get_method(), "POST")
        self.assertIsNone(outbound.data)
        self.assertEqual(timeout, 5)


    def test_url_validation_and_wire_url_use_the_same_exact_text(self):
        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "authenticated-read URL must be canonical HTTPS",
        ):
            AuthenticatedReadHttpRequest(
                url=" https://localhost/iserver/accounts ",
                headers={"Accept": "application/json"},
                timeout_seconds=5,
                method="GET",
            )

        request = AuthenticatedReadHttpRequest(
            url="https://localhost/iserver/accounts",
            headers={"Accept": "application/json"},
            timeout_seconds=5,
            method="get",
        )
        self.assertEqual(request.url, "https://localhost/iserver/accounts")
        self.assertEqual(request.method, "GET")

    def test_executable_text_subclasses_are_rejected_before_virtual_text_methods(self):
        class HostileText(str):
            def strip(self):
                raise AssertionError("hostile strip executed")

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "authenticated-read URL must be canonical HTTPS",
        ):
            AuthenticatedReadHttpRequest(
                url=HostileText("https://localhost/iserver/accounts"),
                headers={"Accept": "application/json"},
                timeout_seconds=5,
                method="GET",
            )

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "authenticated-read method must be canonical text",
        ):
            AuthenticatedReadHttpRequest(
                url="https://localhost/iserver/accounts",
                headers={"Accept": "application/json"},
                timeout_seconds=5,
                method=HostileText("GET"),
            )

    def test_headers_require_exact_inert_mapping_and_canonical_text(self):
        class HostileHeaders(dict):
            def items(self):
                raise AssertionError("hostile items executed")

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "authenticated-read headers must be an exact inert mapping",
        ):
            AuthenticatedReadHttpRequest(
                url="https://localhost/iserver/accounts",
                headers=HostileHeaders({"Accept": "application/json"}),
                timeout_seconds=5,
                method="GET",
            )

        with self.assertRaisesRegex(
            ProviderTransportScopeError,
            "authenticated-read header values must be canonical text",
        ):
            AuthenticatedReadHttpRequest(
                url="https://localhost/iserver/accounts",
                headers={"Accept": " application/json "},
                timeout_seconds=5,
                method="GET",
            )


if __name__ == "__main__":
    unittest.main()
