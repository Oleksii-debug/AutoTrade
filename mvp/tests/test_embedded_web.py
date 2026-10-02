from __future__ import annotations

from hashlib import sha256
import unittest

from mvp.autotrade_mvp.embedded_web import (
    EmbeddedWebError,
    EmbeddedWebHostApplication,
    ImmutableWebAsset,
    ImmutableWebAssetBundle,
)
from mvp.autotrade_mvp.host_network import (
    AuthenticatedHostApplication,
    TransportResponse,
)


ORIGIN = "http://127.0.0.1:8765"
SOURCE = "1" * 40
CONTRACT = "3.0.1"


def _asset(path: str, media_type: str, body: bytes) -> ImmutableWebAsset:
    return ImmutableWebAsset(
        path=path,
        media_type=media_type,
        body=body,
        sha256_hex=sha256(body).hexdigest(),
    )


def _bundle() -> ImmutableWebAssetBundle:
    return ImmutableWebAssetBundle.build(
        source_revision=SOURCE,
        host_api_contract_version=CONTRACT,
        assets=(
            _asset("styles.css", "text/css; charset=utf-8", b"body{}"),
            _asset("index.html", "text/html; charset=utf-8", b"<html></html>"),
            _asset("host-api-routes.js", "text/javascript; charset=utf-8", b"routes"),
            _asset("app.js", "text/javascript; charset=utf-8", b"app"),
        ),
    )


class _ProbeEmbeddedWebHostApplication(EmbeddedWebHostApplication):
    def _dispatch_host(self, *, method, target, headers, body):
        del method, headers, body
        return TransportResponse(
            status=599,
            content_type="application/x-host-delegation",
            body=target.encode("utf-8"),
            headers=(("X-Delegated-To-Canonical-Host", "1"),),
        )


def _application() -> EmbeddedWebHostApplication:
    # Static-route tests intentionally avoid constructing financial/session
    # authority. Production construction still goes through the inherited
    # AuthenticatedHostApplication constructor.
    app = object.__new__(_ProbeEmbeddedWebHostApplication)
    app.public_origin = ORIGIN
    app._web_bundle = _bundle()
    return app


class EmbeddedWebTests(unittest.TestCase):
    def test_adapter_remains_one_canonical_host_application(self):
        self.assertTrue(issubclass(EmbeddedWebHostApplication, AuthenticatedHostApplication))

    def test_asset_identity_is_exact_and_path_bound(self):
        body = b"app"
        digest = sha256(body).hexdigest()
        asset = ImmutableWebAsset(
            "app.js",
            "text/javascript; charset=utf-8",
            body,
            digest,
        )
        self.assertEqual(asset.sha256_hex, digest)
        with self.assertRaisesRegex(EmbeddedWebError, "SHA-256"):
            ImmutableWebAsset(
                "app.js",
                "text/javascript; charset=utf-8",
                body,
                "0" * 64,
            )
        with self.assertRaisesRegex(EmbeddedWebError, "media type"):
            ImmutableWebAsset("app.js", "text/css; charset=utf-8", body, digest)

    def test_asset_paths_reject_traversal_encoding_and_api_collision(self):
        for path in (
            "../app.js",
            "nested/../app.js",
            "/app.js",
            r"nested\app.js",
            "%2e%2e/app.js",
            "app.js?x=1",
            "app.js#x",
            "api/state.js",
            "app.txt",
        ):
            with self.subTest(path=path), self.assertRaises(EmbeddedWebError):
                _asset(path, "text/javascript; charset=utf-8", b"x")

    def test_bundle_identity_is_deterministic_and_forgery_fails(self):
        first = _bundle()
        second = ImmutableWebAssetBundle.build(
            source_revision=SOURCE,
            host_api_contract_version=CONTRACT,
            assets=reversed(first.assets),
        )
        self.assertEqual(first.bundle_sha256, second.bundle_sha256)
        self.assertEqual(tuple(sorted(item.path for item in first.assets)),
                         tuple(item.path for item in first.assets))
        with self.assertRaisesRegex(EmbeddedWebError, "identity"):
            ImmutableWebAssetBundle(
                source_revision=first.source_revision,
                host_api_contract_version=first.host_api_contract_version,
                assets=first.assets,
                bundle_sha256="0" * 64,
            )

    def test_bundle_requires_index_unique_paths_and_canonical_metadata(self):
        app = _asset("app.js", "text/javascript; charset=utf-8", b"app")
        with self.assertRaisesRegex(EmbeddedWebError, "index.html"):
            ImmutableWebAssetBundle.build(
                source_revision=SOURCE,
                host_api_contract_version=CONTRACT,
                assets=(app,),
            )
        index = _asset("index.html", "text/html; charset=utf-8", b"index")
        with self.assertRaisesRegex(EmbeddedWebError, "duplicate"):
            ImmutableWebAssetBundle.build(
                source_revision=SOURCE,
                host_api_contract_version=CONTRACT,
                assets=(index, index),
            )
        with self.assertRaisesRegex(EmbeddedWebError, "Git SHA-1"):
            ImmutableWebAssetBundle.build(
                source_revision="main",
                host_api_contract_version=CONTRACT,
                assets=(index,),
            )

    def test_static_routes_serve_exact_bytes_and_hardened_headers(self):
        app = _application()
        root = app.dispatch(method="GET", target="/", headers={})
        index = app.dispatch(method="GET", target="/index.html", headers={})
        js = app.dispatch(method="GET", target="/app.js", headers={})
        self.assertEqual(root.status, 200)
        self.assertEqual(root.body, index.body)
        self.assertEqual(js.body, b"app")
        headers = dict(js.headers)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-AutoTrade-UI-Source-Revision"], SOURCE)
        self.assertEqual(headers["X-AutoTrade-Host-API-Contract-Version"], CONTRACT)
        self.assertEqual(headers["X-AutoTrade-UI-Bundle-SHA256"], app.web_bundle.bundle_sha256)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("object-src 'none'", headers["Content-Security-Policy"])

    def test_static_route_rejects_query_body_method_cross_origin_and_credentials(self):
        app = _application()
        cases = (
            dict(method="GET", target="/app.js?x=1", headers={}, body=b"", status=400),
            dict(method="POST", target="/app.js", headers={}, body=b"", status=405),
            dict(method="GET", target="/app.js", headers={}, body=b"x", status=400),
            dict(method="GET", target="/app.js", headers={"Origin": "https://example.com"}, body=b"", status=403),
            dict(method="GET", target="/app.js", headers={"Authorization": "AutoTrade-Session secret"}, body=b"", status=400),
            dict(method="GET", target="/app.js", headers={"X-AutoTrade-Actor": "owner"}, body=b"", status=400),
            dict(method="GET", target="/app.js", headers={"Origin": ORIGIN, "origin": ORIGIN}, body=b"", status=400),
        )
        for case in cases:
            with self.subTest(case=case):
                response = app.dispatch(
                    method=case["method"],
                    target=case["target"],
                    headers=case["headers"],
                    body=case["body"],
                )
                self.assertEqual(response.status, case["status"])
                self.assertNotIn(b"secret", response.body)

    def test_exact_same_origin_is_admitted_without_auth_reflection(self):
        app = _application()
        response = app.dispatch(
            method="GET",
            target="/app.js",
            headers={"Origin": ORIGIN, "Accept": "*/*"},
        )
        self.assertEqual(response.status, 200)
        self.assertNotIn(b"Authorization", response.body)
        self.assertNotIn(b"AutoTrade-Session", response.body)

    def test_api_unknown_absolute_and_encoded_targets_delegate_unchanged(self):
        app = _application()
        for target in (
            "/api/v1/state",
            "/unknown",
            "https://example.com/app.js",
            "/%2e%2e/secret.js",
        ):
            with self.subTest(target=target):
                response = app.dispatch(method="GET", target=target, headers={})
                self.assertEqual(response.status, 599)
                self.assertEqual(response.body, target.encode("utf-8"))
                self.assertEqual(
                    dict(response.headers)["X-Delegated-To-Canonical-Host"],
                    "1",
                )


if __name__ == "__main__":
    unittest.main()
