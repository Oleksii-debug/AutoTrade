from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import unittest

from mvp.autotrade_mvp.embedded_web import (
    HOST_API_CONTRACT_VERSION,
    ImmutableWebAsset,
    ImmutableWebAssetBundle,
)
from mvp.autotrade_mvp.host_network import TransportResponse
from mvp.autotrade_mvp.production_embedded_web import (
    ProductionEmbeddedWebDispatch,
)


def _asset(path: str, body: bytes) -> ImmutableWebAsset:
    content_type = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
    }[Path(path).suffix]
    return ImmutableWebAsset(
        path=path,
        body=body,
        sha256_hex=sha256(body).hexdigest(),
        content_type=content_type,
    )


def _bundle() -> ImmutableWebAssetBundle:
    return ImmutableWebAssetBundle(
        source_revision="a" * 40,
        host_api_contract_version=HOST_API_CONTRACT_VERSION,
        assets=(
            _asset("index.html", b"<!doctype html><title>AutoTrade</title>"),
            _asset("app.js", b"export const app = 'autotrade';"),
            _asset("host-api-routes.js", b"export const state = '/api/v1/state';"),
            _asset("styles.css", b"body { font-family: sans-serif; }"),
        ),
    )


class ProductionEmbeddedWebDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[dict[str, object]] = []

        def base_dispatch(**kwargs) -> TransportResponse:
            self.calls.append(dict(kwargs))
            return TransportResponse(
                status=299,
                content_type="application/json; charset=utf-8",
                body=b'{"delegate":true}',
            )

        self.bundle = _bundle()
        self.router = ProductionEmbeddedWebDispatch(
            base_dispatch=base_dispatch,
            public_origin="http://127.0.0.1:8765",
            web_bundle=self.bundle,
        )

    def test_static_root_is_served_without_entering_financial_api_dispatch(self):
        response = self.router.dispatch(
            method="GET",
            target="/",
            headers={"Host": "127.0.0.1:8765"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.body,
            b"<!doctype html><title>AutoTrade</title>",
        )
        self.assertEqual(self.calls, [])
        headers = dict(response.headers)
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("worker-src 'none'", headers["Content-Security-Policy"])

    def test_static_bearer_or_actor_is_rejected_before_delegate(self):
        for header in (
            {"Authorization": "AutoTrade-Session secret"},
            {"X-AutoTrade-Actor": "owner"},
        ):
            with self.subTest(header=header):
                response = self.router.dispatch(
                    method="GET",
                    target="/index.html",
                    headers=header,
                )
                self.assertEqual(response.status, 400)
        self.assertEqual(self.calls, [])

    def test_api_request_delegates_exact_method_target_headers_and_body(self):
        headers = {
            "Authorization": "AutoTrade-Session secret",
            "X-AutoTrade-Actor": "owner",
        }
        body = b'{"command":"unchanged"}'
        response = self.router.dispatch(
            method="POST",
            target="/api/v1/commands",
            headers=headers,
            body=body,
        )
        self.assertEqual(response.status, 299)
        self.assertEqual(
            self.calls,
            [
                {
                    "method": "POST",
                    "target": "/api/v1/commands",
                    "headers": headers,
                    "body": body,
                }
            ],
        )

    def test_post_admission_bundle_alias_mutation_cannot_replace_served_bytes(self):
        original = self.router.dispatch(
            method="GET",
            target="/app.js",
            headers={},
        )
        app_asset = next(
            item for item in self.bundle.assets if item.path == "app.js"
        )
        object.__setattr__(app_asset, "body", b"console.log('forged')")
        object.__setattr__(
            app_asset,
            "sha256_hex",
            sha256(b"console.log('forged')").hexdigest(),
        )
        after = self.router.dispatch(
            method="GET",
            target="/app.js",
            headers={},
        )
        self.assertEqual(after.body, original.body)
        self.assertEqual(after.headers, original.headers)
        self.assertNotIn(b"forged", after.body)

    def test_static_query_and_cross_origin_request_fail_closed(self):
        self.assertEqual(
            self.router.dispatch(
                method="GET",
                target="/index.html?cache=1",
                headers={},
            ).status,
            400,
        )
        self.assertEqual(
            self.router.dispatch(
                method="GET",
                target="/index.html",
                headers={"Origin": "https://attacker.invalid"},
            ).status,
            403,
        )
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
