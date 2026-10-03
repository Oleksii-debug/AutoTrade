from hashlib import sha256
import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import socket
import unittest

from mvp.autotrade_mvp.embedded_web import (
    EmbeddedWebHostApplication,
    HOST_API_CONTRACT_VERSION,
    ImmutableWebAsset,
    ImmutableWebAssetBundle,
    load_immutable_web_bundle,
)
from mvp.autotrade_mvp.host_network import (
    AuthenticatedHostServer,
    header_principal_resolver,
    public_session_reference,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"embedded-web-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


def asset(path: str, body: bytes) -> ImmutableWebAsset:
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


def bundle(*assets: ImmutableWebAsset) -> ImmutableWebAssetBundle:
    return ImmutableWebAssetBundle(
        source_revision="a" * 40,
        host_api_contract_version=HOST_API_CONTRACT_VERSION,
        assets=tuple(assets),
    )


class EmbeddedWebTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.origin = "http://127.0.0.1:8765"
        self.clock = [1000.0]
        vault = ProtectedCredentialVault(
            Path(self.directory.name) / "credentials.json",
            protector=DeterministicProtector(),
        )
        self.boundary = SecurityBoundary(
            allowed_origins={self.origin},
            credential_vault=vault,
            session_authorizer=lambda subject, role, paired_origin: True,
            now=lambda: self.clock[0],
        )
        self.owner = self.boundary.create_session(
            subject="owner",
            role="OWNER",
            origin=self.origin,
            ttl_seconds=600,
        )
        self.web_bundle = bundle(
            asset("index.html", b"<!doctype html><title>AutoTrade</title>"),
            asset("app.js", b"export const app = 'autotrade';"),
            asset("host-api-routes.js", b"export const state = '/api/v1/state';"),
            asset("styles.css", b"body { font-family: sans-serif; }"),
        )
        self.app = EmbeddedWebHostApplication(
            JournalStore(str(Path(self.directory.name) / "journal.sqlite3")),
            security_boundary=self.boundary,
            account_id="paper-account-1",
            environment="PAPER",
            host_id="host-local-1",
            public_origin=self.origin,
            principal_resolver=header_principal_resolver,
            snapshot_provider=self._snapshot,
            now=lambda: "2026-09-25T09:30:00Z",
            web_bundle=self.web_bundle,
        )

    @staticmethod
    def _snapshot(durable, principal):
        return {
            "state_version": durable["state_version"],
            "event_cursor": durable["event_cursor"],
            "server_time": "2026-09-25T09:30:00Z",
            "host_id": "host-local-1",
            "account_id": durable["account_id"],
            "environment": durable["environment"],
            "permission_summary": {
                "actor": principal.actor,
                "session": principal.session,
                "role": principal.role,
            },
            "connection_freshness": {
                "host": "CURRENT",
                "as_of": "2026-09-25T09:30:00Z",
            },
            "portfolio": {},
            "risk": {},
            "strategy": {},
            "jobs": [],
            "reason_codes": [],
        }

    def auth_headers(self):
        return {
            "Authorization": "AutoTrade-Session " + self.owner.token,
            "X-AutoTrade-Actor": "owner",
            "Accept": "application/json",
        }

    def test_asset_requires_exact_hash_immutable_bytes_and_media_type(self):
        body = b"console.log('safe')"
        accepted = asset("nested/app.js", body)
        self.assertEqual(accepted.body, body)

        with self.assertRaisesRegex(ValueError, "sha256"):
            ImmutableWebAsset(
                path="app.js",
                body=body,
                sha256_hex="0" * 64,
                content_type="text/javascript; charset=utf-8",
            )
        with self.assertRaisesRegex(TypeError, "immutable bytes"):
            ImmutableWebAsset(
                path="app.js",
                body=bytearray(body),
                sha256_hex=sha256(body).hexdigest(),
                content_type="text/javascript; charset=utf-8",
            )
        with self.assertRaisesRegex(ValueError, "content type"):
            ImmutableWebAsset(
                path="app.js",
                body=body,
                sha256_hex=sha256(body).hexdigest(),
                content_type="text/html; charset=utf-8",
            )

    def test_asset_paths_reject_traversal_encoding_and_api_namespace(self):
        body = b"x"
        for path in (
            "/index.html",
            "../index.html",
            "nested/../index.html",
            "nested//index.html",
            "nested\\index.html",
            "nested/%2e%2e/index.html",
            "index.html?x=1",
            "index.html#x",
            "api/v1/state.js",
            "API/state.js",
            "readme.txt",
        ):
            with self.subTest(path=path), self.assertRaises((TypeError, ValueError)):
                ImmutableWebAsset(
                    path=path,
                    body=body,
                    sha256_hex=sha256(body).hexdigest(),
                    content_type="text/html; charset=utf-8",
                )

    def test_bundle_identity_is_deterministic_across_manifest_order(self):
        assets = self.web_bundle.assets
        reversed_bundle = ImmutableWebAssetBundle(
            source_revision=self.web_bundle.source_revision,
            host_api_contract_version=self.web_bundle.host_api_contract_version,
            assets=tuple(reversed(assets)),
        )
        self.assertEqual(
            reversed_bundle.bundle_sha256,
            self.web_bundle.bundle_sha256,
        )
        self.assertEqual(
            tuple(item.path for item in reversed_bundle.assets),
            tuple(item.path for item in self.web_bundle.assets),
        )

    def test_bundle_rejects_contract_drift_and_identity_tracks_source_or_content(self):
        with self.assertRaisesRegex(ValueError, "runtime authority"):
            ImmutableWebAssetBundle(
                source_revision="a" * 40,
                host_api_contract_version="5.0.1",
                assets=self.web_bundle.assets,
            )
        changed_source = ImmutableWebAssetBundle(
            source_revision="b" * 40,
            host_api_contract_version=HOST_API_CONTRACT_VERSION,
            assets=self.web_bundle.assets,
        )
        changed_content = bundle(
            asset("index.html", b"changed"),
            *tuple(
                item for item in self.web_bundle.assets
                if item.path != "index.html"
            ),
        )
        self.assertNotEqual(changed_source.bundle_sha256, self.web_bundle.bundle_sha256)
        self.assertNotEqual(changed_content.bundle_sha256, self.web_bundle.bundle_sha256)

    def test_bundle_rejects_missing_core_assets_duplicate_and_noncanonical_identity(self):
        core = {
            item.path: item
            for item in self.web_bundle.assets
        }
        for missing in ("index.html", "app.js", "host-api-routes.js", "styles.css"):
            with self.subTest(missing=missing), self.assertRaisesRegex(
                ValueError,
                "required core assets",
            ):
                bundle(
                    *tuple(
                        item
                        for path, item in core.items()
                        if path != missing
                    )
                )

        duplicate = asset("index.html", b"x")
        with self.assertRaisesRegex(ValueError, "unique"):
            bundle(duplicate, duplicate)
        with self.assertRaisesRegex(ValueError, "source revision"):
            ImmutableWebAssetBundle(
                source_revision="A" * 40,
                host_api_contract_version=HOST_API_CONTRACT_VERSION,
                assets=(duplicate,),
            )
        with self.assertRaisesRegex(ValueError, "contract version"):
            ImmutableWebAssetBundle(
                source_revision="a" * 40,
                host_api_contract_version="v3",
                assets=(duplicate,),
            )

    def test_runtime_host_api_contract_version_matches_canonical_openapi(self):
        root = Path(__file__).resolve().parents[2]
        generated = (root / "mvp" / "autotrade_mvp" / "_generated_common_scalars.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "from ._generated_common_scalars import CONTRACT_VERSION as HOST_API_CONTRACT_VERSION",
            (root / "mvp" / "autotrade_mvp" / "embedded_web.py").read_text(
                encoding="utf-8"
            ),
        )
        marker = 'CONTRACT_VERSION = "'
        generated_version = generated.split(marker, 1)[1].split('"', 1)[0]
        self.assertEqual(HOST_API_CONTRACT_VERSION, generated_version)

        openapi = (
            root
            / "contracts"
            / "openapi"
            / "host-api.yaml"
        ).read_text(encoding="utf-8")
        info = openapi.split("info:", 1)[1].split("\nservers:", 1)[0]
        version_line = next(
            line for line in info.splitlines()
            if line.strip().startswith("version:")
        )
        canonical_version = version_line.split(":", 1)[1].strip()
        self.assertEqual(HOST_API_CONTRACT_VERSION, canonical_version)

    def test_canonical_manifest_round_trip_rejects_missing_extra_or_mutated_bytes(self):
        bodies = {item.path: item.body for item in self.web_bundle.assets}
        loaded = load_immutable_web_bundle(
            self.web_bundle.manifest_bytes,
            dict(bodies),
        )
        self.assertEqual(loaded.bundle_sha256, self.web_bundle.bundle_sha256)
        self.assertEqual(loaded.manifest_bytes, self.web_bundle.manifest_bytes)

        missing = dict(bodies)
        missing.pop("app.js")
        with self.assertRaisesRegex(ValueError, "missing"):
            load_immutable_web_bundle(self.web_bundle.manifest_bytes, missing)

        extra = dict(bodies)
        extra["extra.js"] = b"extra"
        with self.assertRaisesRegex(ValueError, "undeclared"):
            load_immutable_web_bundle(self.web_bundle.manifest_bytes, extra)

        mutated = dict(bodies)
        mutated["app.js"] = b"tampered"
        with self.assertRaisesRegex(ValueError, "(size|sha256)"):
            load_immutable_web_bundle(self.web_bundle.manifest_bytes, mutated)

    def test_manifest_requires_strict_unique_and_canonical_json(self):
        bodies = {item.path: item.body for item in self.web_bundle.assets}
        pretty = json.dumps(
            json.loads(self.web_bundle.manifest_bytes.decode("utf-8")),
            indent=2,
        ).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "canonical JSON"):
            load_immutable_web_bundle(pretty, dict(bodies))

        duplicate = self.web_bundle.manifest_bytes.replace(
            b'{"assets":',
            b'{"schema_version":"1","assets":',
            1,
        )
        with self.assertRaisesRegex(ValueError, "(strict JSON|duplicate)"):
            load_immutable_web_bundle(duplicate, dict(bodies))

    def test_manifest_size_field_is_authoritative(self):
        manifest = json.loads(self.web_bundle.manifest_bytes.decode("utf-8"))
        manifest["assets"][0]["size"] += 1
        mutated = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        bodies = {item.path: item.body for item in self.web_bundle.assets}
        with self.assertRaisesRegex(ValueError, "size"):
            load_immutable_web_bundle(mutated, bodies)

    def test_current_canonical_web_bytes_fit_the_immutable_bundle_contract(self):
        root = Path(__file__).resolve().parents[2] / "web" / "src"
        names = ("index.html", "app.js", "host-api-routes.js", "styles.css")
        canonical_assets = tuple(
            asset(name, (root / name).read_bytes())
            for name in names
        )
        current = ImmutableWebAssetBundle(
            source_revision="c" * 40,
            host_api_contract_version=HOST_API_CONTRACT_VERSION,
            assets=canonical_assets,
        )
        loaded = load_immutable_web_bundle(
            current.manifest_bytes,
            {item.path: item.body for item in canonical_assets},
        )
        self.assertEqual(loaded.bundle_sha256, current.bundle_sha256)
        index = loaded.asset_for_path("/index.html").body.decode("utf-8")
        self.assertIn('src="/host-api-routes.js"', index)
        self.assertIn('src="/app.js"', index)
        self.assertIn('href="/styles.css"', index)

    def test_root_and_asset_routes_return_exact_immutable_bytes(self):
        root = self.app.dispatch(method="GET", target="/", headers={})
        index = self.app.dispatch(method="GET", target="/index.html", headers={})
        script = self.app.dispatch(method="GET", target="/app.js", headers={})
        self.assertEqual(root.status, 200)
        self.assertEqual(root.body, index.body)
        self.assertEqual(root.body, self.web_bundle.asset_for_path("/index.html").body)
        self.assertEqual(script.status, 200)
        self.assertEqual(
            script.body,
            self.web_bundle.asset_for_path("/app.js").body,
        )

    def test_static_responses_bind_bundle_source_contract_and_harden_browser(self):
        response = self.app.dispatch(method="GET", target="/index.html", headers={})
        headers = dict(response.headers)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(
            headers["X-AutoTrade-Web-Bundle"],
            self.web_bundle.bundle_sha256,
        )
        self.assertEqual(headers["X-AutoTrade-Source-Revision"], "a" * 40)
        self.assertEqual(
            headers["X-AutoTrade-Host-Api-Contract"],
            HOST_API_CONTRACT_VERSION,
        )
        self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        csp = headers["Content-Security-Policy"]
        for directive in (
            "default-src 'self'",
            "base-uri 'none'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "connect-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
        ):
            self.assertIn(directive, csp)
        self.assertNotIn("Location", headers)

    def test_static_error_responses_disable_content_sniffing(self):
        response = self.app.dispatch(
            method="GET",
            target="/app.js?v=1",
            headers={},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(
            dict(response.headers)["X-Content-Type-Options"],
            "nosniff",
        )

    def test_static_query_method_and_body_fail_closed(self):
        self.assertEqual(
            self.app.dispatch(method="GET", target="/app.js?v=1", headers={}).status,
            400,
        )
        self.assertEqual(
            self.app.dispatch(method="POST", target="/app.js", headers={}).status,
            405,
        )
        self.assertEqual(
            self.app.dispatch(
                method="GET",
                target="/app.js",
                headers={},
                body=b"unexpected",
            ).status,
            400,
        )

    def test_static_origin_is_exact_and_duplicate_origin_fails(self):
        allowed = self.app.dispatch(
            method="GET",
            target="/styles.css",
            headers={"Origin": self.origin},
        )
        self.assertEqual(allowed.status, 200)
        for origin in (
            "http://127.0.0.1:8766",
            "https://example.com",
        ):
            with self.subTest(origin=origin):
                denied = self.app.dispatch(
                    method="GET",
                    target="/styles.css",
                    headers={"Origin": origin},
                )
                self.assertEqual(denied.status, 403)
        duplicate = self.app.dispatch(
            method="GET",
            target="/styles.css",
            headers={"Origin": self.origin, "origin": self.origin},
        )
        self.assertEqual(duplicate.status, 400)

    def test_static_surface_rejects_accidental_bearer_or_actor_injection(self):
        for headers in (
            {"Authorization": "AutoTrade-Session SECRET-NEVER-STATIC"},
            {"X-AutoTrade-Actor": "owner"},
            {
                "Authorization": "AutoTrade-Session SECRET-NEVER-STATIC",
                "X-AutoTrade-Actor": "owner",
            },
        ):
            response = self.app.dispatch(
                method="GET",
                target="/index.html",
                headers=headers,
            )
            self.assertEqual(response.status, 400)
            rendered = response.body.decode("utf-8")
            self.assertNotIn("SECRET-NEVER-STATIC", rendered)
            self.assertNotIn("owner", rendered)

    def test_request_headers_and_body_are_never_reflected_into_static_content(self):
        response = self.app.dispatch(
            method="GET",
            target="/index.html",
            headers={
                "X-Untrusted": "ATTACKER-MARKER",
                "Referer": "https://example.com/ATTACKER-MARKER",
            },
        )
        self.assertEqual(response.status, 200)
        rendered = response.body.decode("utf-8")
        self.assertNotIn("ATTACKER-MARKER", rendered)
        self.assertEqual(
            response.body,
            self.web_bundle.asset_for_path("/index.html").body,
        )

    def test_encoded_traversal_and_absolute_targets_never_serve_assets(self):
        encoded = self.app.dispatch(
            method="GET",
            target="/%2e%2e/index.html",
            headers=self.auth_headers(),
        )
        absolute = self.app.dispatch(
            method="GET",
            target="http://127.0.0.1:8765/index.html",
            headers=self.auth_headers(),
        )
        self.assertEqual(encoded.status, 404)
        self.assertEqual(absolute.status, 400)

    def test_concrete_host_server_serves_ui_and_api_on_one_origin(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        origin = f"http://127.0.0.1:{port}"

        vault = ProtectedCredentialVault(
            Path(self.directory.name) / f"wire-{port}-credentials.json",
            protector=DeterministicProtector(),
        )
        boundary = SecurityBoundary(
            allowed_origins={origin},
            credential_vault=vault,
            session_authorizer=lambda subject, role, paired_origin: True,
            now=lambda: self.clock[0],
        )
        session = boundary.create_session(
            subject="owner",
            role="OWNER",
            origin=origin,
            ttl_seconds=600,
        )
        app = EmbeddedWebHostApplication(
            JournalStore(str(Path(self.directory.name) / f"wire-{port}.sqlite3")),
            security_boundary=boundary,
            account_id="paper-account-1",
            environment="PAPER",
            host_id="host-local-1",
            public_origin=origin,
            principal_resolver=header_principal_resolver,
            snapshot_provider=self._snapshot,
            now=lambda: "2026-09-25T09:30:00Z",
            web_bundle=self.web_bundle,
        )
        server = AuthenticatedHostServer(("127.0.0.1", port), app)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/index.html")
        ui = conn.getresponse()
        ui_body = ui.read()
        self.assertEqual(ui.status, 200)
        self.assertEqual(
            ui_body,
            self.web_bundle.asset_for_path("/index.html").body,
        )
        self.assertEqual(ui.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(
            ui.getheader("X-AutoTrade-Web-Bundle"),
            self.web_bundle.bundle_sha256,
        )
        self.assertEqual(
            ui.getheader("X-AutoTrade-Host-Api-Contract"),
            HOST_API_CONTRACT_VERSION,
        )
        self.assertIsNone(ui.getheader("Location"))
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "GET",
            "/api/v1/state",
            headers={
                "Authorization": "AutoTrade-Session " + session.token,
                "X-AutoTrade-Actor": "owner",
                "Origin": origin,
                "Accept": "application/json",
            },
        )
        api = conn.getresponse()
        payload = json.loads(api.read().decode("utf-8"))
        self.assertEqual(api.status, 200)
        self.assertEqual(payload["account_id"], "paper-account-1")
        self.assertEqual(payload["environment"], "PAPER")
        self.assertNotIn(session.token, json.dumps(payload))
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "GET",
            "/app.js",
            headers={
                "Authorization": "AutoTrade-Session " + session.token,
                "X-AutoTrade-Actor": "owner",
            },
        )
        leaked = conn.getresponse()
        leaked_body = leaked.read().decode("utf-8")
        self.assertEqual(leaked.status, 400)
        self.assertNotIn(session.token, leaked_body)
        conn.close()

    def test_api_routes_delegate_unchanged_to_canonical_authenticated_host(self):
        denied = self.app.dispatch(
            method="GET",
            target="/api/v1/state",
            headers={},
        )
        self.assertEqual(denied.status, 403)

        allowed = self.app.dispatch(
            method="GET",
            target="/api/v1/state",
            headers=self.auth_headers(),
        )
        self.assertEqual(allowed.status, 200)
        payload = json.loads(allowed.body.decode("utf-8"))
        self.assertEqual(payload["account_id"], "paper-account-1")
        self.assertEqual(payload["environment"], "PAPER")
        self.assertEqual(
            payload["permission_summary"]["session"],
            public_session_reference(self.owner.token),
        )
        self.assertNotIn(self.owner.token, allowed.body.decode("utf-8"))

    def test_unknown_routes_delegate_instead_of_becoming_static_fallback(self):
        response = self.app.dispatch(
            method="GET",
            target="/not-an-asset",
            headers=self.auth_headers(),
        )
        self.assertEqual(response.status, 404)
        self.assertEqual(
            json.loads(response.body.decode("utf-8")),
            {"error": "NOT_FOUND"},
        )

    def test_bundle_is_read_only_and_cannot_alias_api_path(self):
        with self.assertRaises((TypeError, ValueError)):
            asset("api/v1/state.js", b"forged")
        self.assertIsNone(self.web_bundle.asset_for_path("/api/v1/state"))
        self.assertFalse(hasattr(self.web_bundle._routes, "__setitem__"))


if __name__ == "__main__":
    unittest.main()
