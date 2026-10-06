from hashlib import sha256
import http.client
import json
from pathlib import Path
import socket
import time
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from urllib.request import urlopen

from mvp.autotrade_mvp.host_network import (
    AuthenticatedHostApplication,
    AuthenticatedHostServer,
    HostPrincipal,
    header_principal_resolver,
    public_session_reference,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"host-network-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class HostNetworkTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.origin = "http://127.0.0.1:8765"
        self.clock = [1000.0]
        self.boundary = self._boundary(self.origin, "credentials.json")
        self.owner = self.boundary.create_session(
            subject="owner",
            role="OWNER",
            origin=self.origin,
            ttl_seconds=600,
        )
        self.path = str(Path(self.directory.name) / "journal.sqlite3")
        self.app = self._application(
            origin=self.origin,
            boundary=self.boundary,
            session=self.owner,
            path=self.path,
        )

    def _boundary(self, origin, name):
        vault = ProtectedCredentialVault(
            Path(self.directory.name) / name,
            protector=DeterministicProtector(),
        )
        return SecurityBoundary(
            allowed_origins={origin},
            credential_vault=vault,
            session_authorizer=lambda subject, role, paired_origin: True,
            now=lambda: self.clock[0],
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

    def _application(
        self,
        *,
        origin,
        boundary,
        session,
        path,
        max_events=100,
        snapshot_provider=None,
    ):
        del session
        return AuthenticatedHostApplication(
            JournalStore(path),
            security_boundary=boundary,
            account_id="paper-account-1",
            environment="PAPER",
            host_id="host-local-1",
            public_origin=origin,
            principal_resolver=header_principal_resolver,
            snapshot_provider=snapshot_provider or self._snapshot,
            max_events=max_events,
            now=lambda: "2026-09-25T09:30:00Z",
        )

    def headers(self, *, session=None, actor="owner", json_body=False):
        token = self.owner.token if session is None else session
        values = {
            "Authorization": "AutoTrade-Session " + token,
            "X-AutoTrade-Actor": actor,
            "Accept": "application/json",
        }
        if json_body:
            values["Content-Type"] = "application/json"
        return values

    def command(self, **overrides):
        command = {
            "command_id": "11111111-1111-1111-1111-111111111111",
            "expected_state_version": "0",
            "idempotency_key": "host-network-key-1",
            "actor": "owner",
            "session": public_session_reference(self.owner.token),
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "action": "BLOCK_NEW_EXPOSURE",
            "payload": {},
        }
        command.update(overrides)
        return command

    @staticmethod
    def body(response):
        return json.loads(response.body.decode("utf-8"))

    def post(self, command, *, headers=None):
        return self.app.dispatch(
            method="POST",
            target="/api/v1/commands",
            headers=headers or self.headers(json_body=True),
            body=json.dumps(command).encode("utf-8"),
        )

    def test_health_is_nonsensitive_and_does_not_require_session(self):
        response = self.app.dispatch(
            method="GET",
            target="/api/v1/health",
            headers={},
        )
        self.assertEqual(response.status, 200)
        payload = self.body(response)
        self.assertEqual(payload["component"], "HOST_NETWORK")
        self.assertEqual(payload["status"], "READY")
        rendered = response.body.decode("utf-8")
        self.assertNotIn("paper-account-1", rendered)
        self.assertNotIn(self.owner.token, rendered)

    def test_state_requires_authenticated_principal_and_matches_durable_scope(self):
        denied = self.app.dispatch(
            method="GET",
            target="/api/v1/state",
            headers={},
        )
        self.assertEqual(denied.status, 403)

        response = self.app.dispatch(
            method="GET",
            target="/api/v1/state",
            headers=self.headers(),
        )
        self.assertEqual(response.status, 200)
        payload = self.body(response)
        self.assertEqual(payload["host_id"], "host-local-1")
        self.assertEqual(payload["account_id"], "paper-account-1")
        self.assertEqual(payload["environment"], "PAPER")
        self.assertEqual(payload["state_version"], "0")
        self.assertEqual(payload["permission_summary"]["actor"], "owner")
        self.assertEqual(payload["permission_summary"]["role"], "OWNER")
        self.assertEqual(
            payload["permission_summary"]["session"],
            public_session_reference(self.owner.token),
        )
        self.assertNotIn(self.owner.token, response.body.decode("utf-8"))

    def test_snapshot_role_cannot_exceed_authenticated_session_role(self):
        observer = self.boundary.create_session(
            subject="owner",
            role="OBSERVER",
            origin=self.origin,
            ttl_seconds=600,
        )

        def forged_role(durable, principal):
            value = dict(self._snapshot(durable, principal))
            value["permission_summary"] = dict(value["permission_summary"])
            value["permission_summary"]["role"] = "OWNER"
            return value

        app = self._application(
            origin=self.origin,
            boundary=self.boundary,
            session=observer,
            path=self.path,
            snapshot_provider=forged_role,
        )
        response = app.dispatch(
            method="GET",
            target="/api/v1/state",
            headers=self.headers(session=observer.token),
        )
        self.assertEqual(response.status, 400)

    def test_snapshot_permission_summary_rejects_missing_or_extra_fields(self):
        for case in ("missing", "extra"):
            with self.subTest(case=case):
                def invalid(durable, principal, case=case):
                    value = dict(self._snapshot(durable, principal))
                    permission = dict(value["permission_summary"])
                    if case == "missing":
                        permission.pop("role")
                    else:
                        permission["unexpected"] = "value"
                    value["permission_summary"] = permission
                    return value

                app = self._application(
                    origin=self.origin,
                    boundary=self.boundary,
                    session=self.owner,
                    path=self.path,
                    snapshot_provider=invalid,
                )
                response = app.dispatch(
                    method="GET",
                    target="/api/v1/state",
                    headers=self.headers(),
                )
                self.assertEqual(response.status, 400)

    def test_snapshot_capabilities_must_be_unique_canonical_strings(self):
        invalid_values = (
            ["READ", "READ"],
            [" READ "],
            ["READ", 1],
        )
        for capabilities in invalid_values:
            with self.subTest(capabilities=capabilities):
                def invalid(durable, principal, capabilities=capabilities):
                    value = dict(self._snapshot(durable, principal))
                    permission = dict(value["permission_summary"])
                    permission["capabilities"] = capabilities
                    value["permission_summary"] = permission
                    return value

                app = self._application(
                    origin=self.origin,
                    boundary=self.boundary,
                    session=self.owner,
                    path=self.path,
                    snapshot_provider=invalid,
                )
                response = app.dispatch(
                    method="GET",
                    target="/api/v1/state",
                    headers=self.headers(),
                )
                self.assertEqual(response.status, 400)

    def test_snapshot_capabilities_accept_unique_canonical_strings(self):
        def valid(durable, principal):
            value = dict(self._snapshot(durable, principal))
            permission = dict(value["permission_summary"])
            permission["capabilities"] = ["READ_STATE", "BLOCK_NEW_EXPOSURE"]
            value["permission_summary"] = permission
            return value

        app = self._application(
            origin=self.origin,
            boundary=self.boundary,
            session=self.owner,
            path=self.path,
            snapshot_provider=valid,
        )
        response = app.dispatch(
            method="GET",
            target="/api/v1/state",
            headers=self.headers(),
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            self.body(response)["permission_summary"]["role"],
            "OWNER",
        )

    def test_snapshot_projector_has_no_bearer_and_cannot_serialize_closure_leak(self):
        observed = {}

        def leaking(durable, principal):
            observed["has_token"] = hasattr(principal, "token")
            value = dict(self._snapshot(durable, principal))
            value["portfolio"] = {"accidental": self.owner.token}
            return value

        app = self._application(
            origin=self.origin,
            boundary=self.boundary,
            session=self.owner,
            path=self.path,
            snapshot_provider=leaking,
        )
        response = app.dispatch(
            method="GET",
            target="/api/v1/state",
            headers=self.headers(),
        )
        self.assertFalse(observed["has_token"])
        self.assertEqual(response.status, 400)
        self.assertNotIn(self.owner.token, response.body.decode("utf-8"))

    def test_snapshot_projector_cannot_forge_durable_state_or_scope(self):
        def forged(durable, principal):
            value = dict(self._snapshot(durable, principal))
            value["state_version"] = "999"
            return value

        app = self._application(
            origin=self.origin,
            boundary=self.boundary,
            session=self.owner,
            path=self.path,
            snapshot_provider=forged,
        )
        response = app.dispatch(
            method="GET",
            target="/api/v1/state",
            headers=self.headers(),
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.body(response), {"error": "INVALID_REQUEST"})

    def test_command_uses_authenticated_actor_session_and_action_aware_store(self):
        response = self.post(self.command())
        self.assertEqual(response.status, 200)
        result = self.body(response)
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertEqual(result["state_version"], "1")
        self.assertIsInstance(result["operation_id"], str)

        state = self.app.store.snapshot()
        self.assertEqual(state["state_version"], "1")
        events = self.app.store.events_after(0)
        self.assertEqual(len(events), 1)
        rendered = json.dumps(events[0].payload, sort_keys=True)
        self.assertNotIn(self.owner.token, rendered)
        self.assertNotIn("idempotency_key", rendered)
        command_rendered = json.dumps(self.command(), sort_keys=True)
        self.assertNotIn(self.owner.token, command_rendered)
        for suffix in ("", "-wal", "-shm"):
            durable_path = Path(self.path + suffix)
            if durable_path.exists():
                self.assertNotIn(
                    self.owner.token.encode("utf-8"),
                    durable_path.read_bytes(),
                )

    def test_non_event_routes_reject_query_and_get_body_before_auth_mutation(self):
        query = self.app.dispatch(
            method="GET",
            target="/api/v1/state?unexpected=1",
            headers=self.headers(),
        )
        self.assertEqual(query.status, 400)
        self.assertEqual(self.body(query), {"error": "INVALID_QUERY"})
        self.assertEqual(self.app.store.state_version, 0)

        body = self.app.dispatch(
            method="GET",
            target="/api/v1/state",
            headers=self.headers(),
            body=b"{}",
        )
        self.assertEqual(body.status, 400)
        self.assertEqual(self.body(body), {"error": "UNEXPECTED_REQUEST_BODY"})
        self.assertEqual(self.app.store.state_version, 0)

    def test_header_body_identity_mismatch_is_rejected_before_mutation(self):
        wrong_actor = self.post(
            self.command(),
            headers=self.headers(actor="other", json_body=True),
        )
        self.assertEqual(wrong_actor.status, 403)
        self.assertEqual(self.app.store.state_version, 0)

        wrong_session = self.command(session="forged")
        denied = self.post(wrong_session)
        self.assertEqual(denied.status, 403)
        self.assertEqual(self.app.store.state_version, 0)

    def test_expired_session_is_rejected_before_mutation(self):
        self.clock[0] = 2000.0
        response = self.post(self.command())
        self.assertEqual(response.status, 403)
        self.assertEqual(self.app.store.state_version, 0)

    def test_ambiguous_json_body_is_rejected_before_mutation(self):
        session = public_session_reference(self.owner.token)
        duplicate = (
            '{"command_id":"11111111-1111-1111-1111-111111111111",'
            '"command_id":"22222222-2222-2222-2222-222222222222",'
            '"expected_state_version":"0",'
            '"idempotency_key":"host-network-key-ambiguous",'
            '"actor":"owner",'
            f'"session":"{session}",'
            '"account_id":"paper-account-1",'
            '"environment":"PAPER",'
            '"action":"BLOCK_NEW_EXPOSURE",'
            '"payload":{}}'
        ).encode("utf-8")
        response = self.app.dispatch(
            method="POST",
            target="/api/v1/commands",
            headers=self.headers(json_body=True),
            body=duplicate,
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.app.store.state_version, 0)

        non_finite = dict(self.command())
        non_finite["payload"] = {"risk": float("nan")}
        response = self.app.dispatch(
            method="POST",
            target="/api/v1/commands",
            headers=self.headers(json_body=True),
            body=json.dumps(non_finite).encode("utf-8"),
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.app.store.state_version, 0)

    def test_paired_but_different_origin_session_cannot_cross_listener_origin(self):
        other = "http://127.0.0.1:8766"
        self.boundary.pair_origin(
            self.owner.token,
            origin=self.origin,
            new_origin=other,
        )
        foreign = self.boundary.create_session(
            subject="owner",
            role="OWNER",
            origin=other,
            ttl_seconds=600,
        )
        response = self.app.dispatch(
            method="GET",
            target="/api/v1/state",
            headers=self.headers(session=foreign.token),
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.app.store.state_version, 0)

    def test_lost_response_exact_retry_recovers_original_without_duplicate_event(self):
        command = self.command()
        first = self.post(command)
        self.assertEqual(first.status, 200)
        first_payload = self.body(first)

        # Simulate the client never receiving/retaining the first HTTP response.
        retried = self.post(command)
        self.assertEqual(retried.status, 200)
        self.assertEqual(self.body(retried), first_payload)
        self.assertEqual(self.app.store.state_version, 1)
        self.assertEqual(len(self.app.store.events_after(0)), 1)

    def test_changed_payload_under_same_idempotency_key_conflicts(self):
        accepted = self.post(self.command())
        self.assertEqual(accepted.status, 200)
        changed = self.post(self.command(payload={"different": True}))
        self.assertEqual(changed.status, 409)
        result = self.body(changed)
        self.assertEqual(result["status"], "CONFLICT")
        self.assertIn("idempotency_key_conflict", result["reason_codes"])
        self.assertEqual(self.app.store.state_version, 1)

    def test_operation_is_read_only_and_survives_application_restart(self):
        accepted = self.body(self.post(self.command()))
        operation_id = accepted["operation_id"]
        restarted = self._application(
            origin=self.origin,
            boundary=self.boundary,
            session=self.owner,
            path=self.path,
        )
        response = restarted.dispatch(
            method="GET",
            target="/api/v1/operations/" + operation_id,
            headers=self.headers(),
        )
        self.assertEqual(response.status, 200)
        operation = self.body(response)
        self.assertEqual(operation["operation_id"], operation_id)
        self.assertEqual(operation["phase"], "QUEUED")
        self.assertIn(
            "financial_outcome_not_completed",
            operation["remaining_uncertainty"],
        )
        self.assertEqual(restarted.store.state_version, 1)

    def test_server_executes_authority_after_accepted_response(self):
        origin, session, app, port = self._network_fixture()
        command_body = json.dumps(self._wire_command(session)).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        self.addCleanup(conn.close)

        conn.request(
            "POST",
            "/api/v1/commands",
            body=command_body,
            headers=self._wire_headers(session, origin=origin, json_body=True),
        )
        accepted_response = conn.getresponse()
        accepted = json.loads(accepted_response.read().decode("utf-8"))
        self.assertEqual(accepted_response.status, 200)
        self.assertEqual(accepted["status"], "ACCEPTED")
        operation_id = accepted["operation_id"]

        # Reuse the same HTTP connection.  The server cannot read this request
        # until the first request handler has emitted ACCEPTED and completed
        # its separate authority-operation pump.
        conn.request(
            "GET",
            "/api/v1/operations/" + operation_id,
            headers=self._wire_headers(session, origin=origin),
        )
        operation_response = conn.getresponse()
        operation = json.loads(operation_response.read().decode("utf-8"))
        self.assertEqual(operation_response.status, 200)
        self.assertEqual(operation["phase"], "SUCCEEDED")
        self.assertEqual(operation["remaining_uncertainty"], [])
        self.assertEqual(
            operation["affected_refs"],
            ["authority-new-exposure-block:paper-account-1:PAPER"],
        )

        authority_events = JournalStore(
            str(Path(self.directory.name) / f"network-{port}.sqlite3")
        ).load_events("authority_state", "canonical")
        self.assertEqual(
            [event["event_type"] for event in authority_events],
            ["AuthorityNewExposureBlocked"],
        )
        self.assertEqual(app.store.get_operation(operation_id).phase, "SUCCEEDED")

    def test_server_startup_resumes_running_authority_without_duplicate_mutation(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        origin = f"http://127.0.0.1:{port}"
        boundary = self._boundary(origin, f"restart-{port}-credentials.json")
        session = boundary.create_session(
            subject="owner",
            role="OWNER",
            origin=origin,
            ttl_seconds=600,
        )
        path = str(Path(self.directory.name) / f"restart-{port}.sqlite3")
        initial = self._application(
            origin=origin,
            boundary=boundary,
            session=session,
            path=path,
        )
        accepted_response = initial.dispatch(
            method="POST",
            target="/api/v1/commands",
            headers=self._wire_headers(session, origin=origin, json_body=True),
            body=json.dumps(self._wire_command(session)).encode("utf-8"),
        )
        accepted = self.body(accepted_response)
        operation_id = accepted["operation_id"]
        initial.store.update_operation(
            operation_id,
            "RUNNING",
            remaining_uncertainty=("authority_commit_pending",),
        )
        self.assertEqual(initial.store.get_operation(operation_id).phase, "RUNNING")

        restarted = self._application(
            origin=origin,
            boundary=boundary,
            session=session,
            path=path,
        )
        server = AuthenticatedHostServer(("127.0.0.1", port), restarted)
        self.addCleanup(server.server_close)

        completed = restarted.store.get_operation(operation_id)
        self.assertEqual(completed.phase, "SUCCEEDED")
        authority_events = JournalStore(path).load_events(
            "authority_state",
            "canonical",
        )
        self.assertEqual(
            [event["event_type"] for event in authority_events],
            ["AuthorityNewExposureBlocked"],
        )

        self.assertEqual(
            restarted.resume_authority_operations(),
            (),
        )
        self.assertEqual(
            len(
                JournalStore(path).load_events(
                    "authority_state",
                    "canonical",
                )
            ),
            1,
        )

    def test_event_cursor_gap_requires_canonical_resnapshot(self):
        app = self._application(
            origin=self.origin,
            boundary=self.boundary,
            session=self.owner,
            path=self.path,
            max_events=2,
        )
        first = app.dispatch(
            method="POST",
            target="/api/v1/commands",
            headers=self.headers(json_body=True),
            body=json.dumps(self.command()).encode("utf-8"),
        )
        operation_id = self.body(first)["operation_id"]
        app.store.update_operation(
            operation_id,
            "RUNNING",
            remaining_uncertainty=("provider_response_pending",),
        )
        app.store.update_operation(
            operation_id,
            "WAITING_EXTERNAL",
            remaining_uncertainty=("provider_response_pending",),
        )
        response = app.dispatch(
            method="GET",
            target="/api/v1/events?after=0",
            headers=self.headers(),
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(
            self.body(response),
            {
                "error": "EVENT_CURSOR_GAP",
                "resnapshot": "/api/v1/state",
            },
        )

    def test_events_support_existing_json_client_and_canonical_sse_without_secrets(self):
        self.post(self.command())
        json_response = self.app.dispatch(
            method="GET",
            target="/api/v1/events?after=0",
            headers=self.headers(),
        )
        self.assertEqual(json_response.status, 200)
        events = self.body(json_response)
        self.assertEqual(events[0]["cursor"], "1")
        self.assertEqual(events[0]["kind"], "COMMAND_ACCEPTED")
        self.assertNotIn(self.owner.token, json_response.body.decode("utf-8"))

        sse_headers = self.headers()
        sse_headers["Accept"] = "text/event-stream"
        sse = self.app.dispatch(
            method="GET",
            target="/api/v1/events?after=0",
            headers=sse_headers,
        )
        self.assertEqual(sse.status, 200)
        self.assertTrue(sse.content_type.startswith("text/event-stream"))
        text = sse.body.decode("utf-8")
        self.assertIn("id: 1\n", text)
        self.assertIn("event: COMMAND_ACCEPTED\n", text)
        self.assertNotIn(self.owner.token, text)

    def test_plain_http_cannot_bind_non_loopback(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            AuthenticatedHostServer(("0.0.0.0", 8765), self.app)

    def test_listener_port_must_match_authenticated_public_origin(self):
        with self.assertRaisesRegex(ValueError, "listener"):
            AuthenticatedHostServer(("127.0.0.1", 0), self.app)

    def _network_fixture(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        origin = f"http://127.0.0.1:{port}"
        boundary = self._boundary(origin, f"network-{port}-credentials.json")
        session = boundary.create_session(
            subject="owner",
            role="OWNER",
            origin=origin,
            ttl_seconds=600,
        )
        app = self._application(
            origin=origin,
            boundary=boundary,
            session=session,
            path=str(Path(self.directory.name) / f"network-{port}.sqlite3"),
        )
        server = AuthenticatedHostServer(("127.0.0.1", port), app)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        return origin, session, app, port

    @staticmethod
    def _wire_headers(session, *, origin=None, json_body=False):
        headers = {
            "Authorization": "AutoTrade-Session " + session.token,
            "X-AutoTrade-Actor": "owner",
            "Accept": "application/json",
        }
        if origin is not None:
            headers["Origin"] = origin
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _wire_command(self, session, **overrides):
        value = {
            "command_id": "22222222-2222-2222-2222-222222222222",
            "expected_state_version": "0",
            "idempotency_key": "host-network-wire-key-1",
            "actor": "owner",
            "session": public_session_reference(session.token),
            "account_id": "paper-account-1",
            "environment": "PAPER",
            "action": "BLOCK_NEW_EXPOSURE",
            "payload": {},
        }
        value.update(overrides)
        return value

    def test_concrete_server_binds_browser_origin_and_explicit_native_channel(self):
        origin, session, app, port = self._network_fixture()
        foreign = f"http://127.0.0.1:{port + 1}"

        for supplied_origin, expected in (
            (foreign, 403),
            (origin, 200),
            (None, 200),
        ):
            with self.subTest(route="state", origin=supplied_origin):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request(
                    "GET",
                    "/api/v1/state",
                    headers=self._wire_headers(session, origin=supplied_origin),
                )
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, expected)
                conn.close()

        body = json.dumps(self._wire_command(session)).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST",
            "/api/v1/commands",
            body=body,
            headers=self._wire_headers(
                session,
                origin=foreign,
                json_body=True,
            ),
        )
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 403)
        self.assertEqual(app.store.state_version, 0)
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST",
            "/api/v1/commands",
            body=body,
            headers=self._wire_headers(
                session,
                origin=origin,
                json_body=True,
            ),
        )
        response = conn.getresponse()
        first_payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(first_payload["status"], "ACCEPTED")
        conn.close()

        # The concrete handler resumes accepted authority work only after
        # writing ACCEPTED. Poll the public operation resource with a bounded
        # timeout instead of racing the server thread through the internal store.
        operation_id = first_payload["operation_id"]
        deadline = time.monotonic() + 2.0
        operation_payload = None
        while time.monotonic() < deadline:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request(
                "GET",
                "/api/v1/operations/" + operation_id,
                headers=self._wire_headers(session, origin=origin),
            )
            operation_response = conn.getresponse()
            operation_payload = json.loads(
                operation_response.read().decode("utf-8")
            )
            conn.close()
            self.assertEqual(operation_response.status, 200)
            if operation_payload["phase"] in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            }:
                break
            time.sleep(0.01)

        self.assertIsNotNone(operation_payload)
        self.assertEqual(operation_payload["phase"], "SUCCEEDED")

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "GET",
            "/api/v1/state",
            headers=self._wire_headers(session, origin=origin),
        )
        state_response = conn.getresponse()
        state_payload = json.loads(state_response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(state_response.status, 200)
        current_state_version = str(state_payload["state_version"])

        native = self._wire_command(
            session,
            command_id="33333333-3333-3333-3333-333333333333",
            expected_state_version=current_state_version,
            idempotency_key="host-network-wire-key-2",
        )
        # Signal when the exact post-ACCEPTED authority handler has *returned*,
        # not when a concurrent read merely observes the journal's SUCCEEDED
        # phase while the handler may still hold SQLite resources. This is a
        # test-local instrument, not a production completion/authority channel.
        resume_finished = Event()
        resumed_ids: list[str] = []
        original_resume = app.resume_authority_operations

        def observed_resume():
            try:
                completed = original_resume()
                resumed_ids.extend(completed)
                return completed
            finally:
                resume_finished.set()

        app.resume_authority_operations = observed_resume

        native_body = json.dumps(native).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST",
            "/api/v1/commands",
            body=native_body,
            headers=self._wire_headers(session, json_body=True),
        )
        response = conn.getresponse()
        native_payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(native_payload["status"], "ACCEPTED")
        self.assertEqual(
            int(native_payload["state_version"]),
            int(current_state_version) + 1,
        )
        conn.close()

        # The exact handler completion signal is raised after
        # resume_authority_operations() returns, with all its SQLite contexts
        # closed. No wall-clock polling/sleeps or cleanup suppression. An
        # externally imposed timeout only prevents a hung test from hanging CI.
        native_operation_id = native_payload["operation_id"]
        self.assertTrue(
            resume_finished.wait(timeout=10.0),
            "native post-ACCEPTED handler failed to drain",
        )
        self.assertIn(native_operation_id, resumed_ids)

        # Still prove durable financial authority via the public endpoint;
        # the private completion signal alone is not permission/evidence.
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "GET",
            "/api/v1/operations/" + native_operation_id,
            headers=self._wire_headers(session, origin=origin),
        )
        operation_response = conn.getresponse()
        native_operation = json.loads(operation_response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(operation_response.status, 200)
        self.assertEqual(native_operation["operation_id"], native_operation_id)
        self.assertEqual(native_operation["phase"], "SUCCEEDED")

    def test_concrete_server_rejects_duplicate_sensitive_headers_before_dispatch(self):
        origin, session, app, port = self._network_fixture()
        authorization = "AutoTrade-Session " + session.token

        def request_with_duplicate(method, path, duplicate_name, duplicate_values, body=b""):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.putrequest(method, path)
            conn.putheader("Authorization", authorization)
            conn.putheader("X-AutoTrade-Actor", "owner")
            conn.putheader("Origin", origin)
            conn.putheader("Accept", "application/json")
            if body:
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(body)))
            for value in duplicate_values:
                conn.putheader(duplicate_name, value)
            conn.endheaders(body if body else None)
            response = conn.getresponse()
            payload = response.read()
            status = response.status
            conn.close()
            return status, payload

        read_duplicates = {
            "Authorization": (authorization,),
            "X-AutoTrade-Actor": ("owner",),
            "Origin": (origin,),
            "Host": (f"127.0.0.1:{port}",),
            "Accept": ("application/json",),
        }
        for name, extra in read_duplicates.items():
            with self.subTest(route="state", header=name):
                status, _ = request_with_duplicate(
                    "GET",
                    "/api/v1/state",
                    name,
                    extra,
                )
                self.assertEqual(status, 400)

        command_body = json.dumps(self._wire_command(session)).encode("utf-8")
        post_duplicates = {
            "Authorization": (authorization,),
            "X-AutoTrade-Actor": ("owner",),
            "Origin": (origin,),
            "Host": (f"127.0.0.1:{port}",),
            "Accept": ("application/json",),
            "Content-Type": ("application/json",),
            "Content-Length": (str(len(command_body)),),
        }
        for name, extra in post_duplicates.items():
            with self.subTest(route="commands", header=name):
                status, payload = request_with_duplicate(
                    "POST",
                    "/api/v1/commands",
                    name,
                    extra,
                    command_body,
                )
                self.assertEqual(status, 400)
                self.assertNotIn(session.token.encode("utf-8"), payload)
                self.assertEqual(app.store.state_version, 0)

    def test_concrete_server_rejects_missing_or_mismatched_host_before_dispatch(self):
        origin, session, app, port = self._network_fixture()
        authorization = "AutoTrade-Session " + session.token

        def raw_host(host_value):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.putrequest("GET", "/api/v1/state", skip_host=True)
            if host_value is not None:
                conn.putheader("Host", host_value)
            conn.putheader("Authorization", authorization)
            conn.putheader("X-AutoTrade-Actor", "owner")
            conn.putheader("Origin", origin)
            conn.endheaders()
            response = conn.getresponse()
            response.read()
            status = response.status
            conn.close()
            return status

        self.assertEqual(raw_host(None), 400)
        self.assertEqual(raw_host(f"127.0.0.1:{port + 1}"), 403)
        self.assertEqual(app.store.state_version, 0)

    def test_concrete_server_rejects_transfer_encoding_before_dispatch(self):
        origin, session, app, port = self._network_fixture()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.putrequest("POST", "/api/v1/commands")
        conn.putheader("Authorization", "AutoTrade-Session " + session.token)
        conn.putheader("X-AutoTrade-Actor", "owner")
        conn.putheader("Origin", origin)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.status, 400)
        self.assertEqual(app.store.state_version, 0)
        conn.close()

    def test_concrete_loopback_server_serves_health_without_request_logging(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        origin = f"http://127.0.0.1:{port}"
        boundary = self._boundary(origin, "network-credentials.json")
        session = boundary.create_session(
            subject="owner",
            role="OWNER",
            origin=origin,
            ttl_seconds=600,
        )
        app = self._application(
            origin=origin,
            boundary=boundary,
            session=session,
            path=str(Path(self.directory.name) / "network.sqlite3"),
        )
        server = AuthenticatedHostServer(("127.0.0.1", port), app)
        self.addCleanup(server.server_close)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        with urlopen(origin + "/api/v1/health", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["component"], "HOST_NETWORK")
            self.assertNotIn("paper-account-1", json.dumps(payload))


    def test_header_and_principal_text_reject_subclasses_before_callbacks(self):
        callbacks = []

        class HostileText(str):
            def __str__(self):
                callbacks.append("str")
                raise AssertionError("hostile text __str__ callback must not run")

            def __bool__(self):
                callbacks.append("bool")
                raise AssertionError("hostile text truthiness callback must not run")

            def strip(self, *args, **kwargs):
                callbacks.append("strip")
                raise AssertionError("hostile text strip callback must not run")

            def startswith(self, *args, **kwargs):
                callbacks.append("startswith")
                raise AssertionError("hostile text startswith callback must not run")

            def encode(self, *args, **kwargs):
                callbacks.append("encode")
                raise AssertionError("hostile text encode callback must not run")

            def __eq__(self, other):
                callbacks.append("eq")
                raise AssertionError("hostile text equality callback must not run")

            def __hash__(self):
                callbacks.append("hash")
                raise AssertionError("hostile text hash callback must not run")

        valid_token = self.owner.token
        valid_session = public_session_reference(valid_token)

        with self.assertRaises(ValueError):
            public_session_reference(HostileText(valid_token))
        self.assertEqual(callbacks, [])

        for field, value in (
            ("actor", HostileText("owner")),
            ("token", HostileText(valid_token)),
            ("session", HostileText(valid_session)),
        ):
            with self.subTest(principal_field=field):
                callbacks.clear()
                values = {
                    "actor": "owner",
                    "token": valid_token,
                    "session": valid_session,
                }
                values[field] = value
                with self.assertRaises(ValueError):
                    HostPrincipal(**values)
                self.assertEqual(callbacks, [])

        valid_authorization = "AutoTrade-Session " + valid_token
        for headers in (
            {
                "x-autotrade-actor": HostileText("owner"),
                "authorization": valid_authorization,
            },
            {
                "x-autotrade-actor": "owner",
                "authorization": HostileText(valid_authorization),
            },
        ):
            callbacks.clear()
            with self.assertRaises(PermissionError):
                header_principal_resolver(headers, self.origin)
            self.assertEqual(callbacks, [])

        callbacks.clear()
        response = self.app.dispatch(
            method="GET",
            target="/api/v1/health",
            headers={"X-Probe": HostileText("value")},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.body(response), {"error": "INVALID_REQUEST"})
        self.assertEqual(callbacks, [])

        class HostileHeaderMap(dict):
            def items(self):
                return [(HostileText("X-Probe"), "value")]

        callbacks.clear()
        response = self.app.dispatch(
            method="GET",
            target="/api/v1/health",
            headers=HostileHeaderMap(),
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.body(response), {"error": "INVALID_REQUEST"})
        self.assertEqual(callbacks, [])


    def test_host_id_configuration_rejects_str_subclass_without_strip_callback(self):
        callbacks = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                callbacks.append("strip")
                raise AssertionError("host_id strip callback must not run")

        with self.assertRaisesRegex(ValueError, "host_id is required"):
            AuthenticatedHostApplication(
                JournalStore(str(Path(self.directory.name) / "hostile-host-id.sqlite3")),
                security_boundary=self.boundary,
                account_id="paper-account-1",
                environment="PAPER",
                host_id=HostileText("host-local-1"),
                public_origin=self.origin,
                principal_resolver=header_principal_resolver,
                snapshot_provider=self._snapshot,
                now=lambda: "2026-09-25T09:30:00Z",
            )

        self.assertEqual(callbacks, [])


    def test_snapshot_scope_identity_rejects_str_subclasses_before_comparison(self):
        callbacks = []

        class HostileText(str):
            def __eq__(self, other):
                callbacks.append("eq")
                raise AssertionError("snapshot identity equality callback must not run")

            def __ne__(self, other):
                callbacks.append("ne")
                raise AssertionError("snapshot identity inequality callback must not run")

        for field in ("account_id", "host_id"):
            with self.subTest(field=field):
                callbacks.clear()

                def hostile_snapshot(durable, principal, *, _field=field):
                    payload = dict(self._snapshot(durable, principal))
                    payload[_field] = HostileText(str(payload[_field]))
                    return payload

                app = self._application(
                    origin=self.origin,
                    boundary=self.boundary,
                    session=self.owner,
                    path=str(
                        Path(self.directory.name)
                        / f"snapshot-hostile-{field}.sqlite3"
                    ),
                    snapshot_provider=hostile_snapshot,
                )
                response = app.dispatch(
                    method="GET",
                    target="/api/v1/state",
                    headers=self.headers(),
                )
                self.assertEqual(response.status, 400)
                self.assertEqual(
                    self.body(response),
                    {"error": "INVALID_REQUEST"},
                )
                self.assertEqual(callbacks, [])


    def test_snapshot_identity_metadata_rejects_str_subclasses_without_callbacks(self):
        callbacks = []

        class HostileText(str):
            def __bool__(self):
                callbacks.append("bool")
                raise AssertionError("snapshot text truthiness callback must not run")

            def __eq__(self, other):
                callbacks.append("eq")
                raise AssertionError("snapshot text equality callback must not run")

            def __ne__(self, other):
                callbacks.append("ne")
                raise AssertionError("snapshot text inequality callback must not run")

            def strip(self, *args, **kwargs):
                callbacks.append("strip")
                raise AssertionError("snapshot text strip callback must not run")

        cases = (
            ("actor", "owner"),
            ("session", public_session_reference(self.owner.token)),
            ("role", "OWNER"),
            ("capability", "READ"),
            ("reason_code", "diagnostic"),
            ("server_time", "2026-09-25T09:30:00Z"),
        )
        for target, raw_value in cases:
            with self.subTest(target=target):
                callbacks.clear()

                def hostile_snapshot(durable, principal, *, _target=target, _value=raw_value):
                    payload = dict(self._snapshot(durable, principal))
                    hostile = HostileText(_value)
                    if _target in {"actor", "session", "role"}:
                        payload["permission_summary"] = dict(payload["permission_summary"])
                        payload["permission_summary"][_target] = hostile
                    elif _target == "capability":
                        payload["permission_summary"] = dict(payload["permission_summary"])
                        payload["permission_summary"]["capabilities"] = [hostile]
                    elif _target == "reason_code":
                        payload["reason_codes"] = [hostile]
                    else:
                        payload["server_time"] = hostile
                    return payload

                app = self._application(
                    origin=self.origin,
                    boundary=self.boundary,
                    session=self.owner,
                    path=str(
                        Path(self.directory.name)
                        / f"snapshot-hostile-metadata-{target}.sqlite3"
                    ),
                    snapshot_provider=hostile_snapshot,
                )
                response = app.dispatch(
                    method="GET",
                    target="/api/v1/state",
                    headers=self.headers(),
                )
                self.assertEqual(response.status, 400)
                self.assertEqual(self.body(response), {"error": "INVALID_REQUEST"})
                self.assertEqual(callbacks, [])


    def test_snapshot_sequence_identity_cannot_be_type_coerced(self):
        for field in ("state_version", "event_cursor"):
            with self.subTest(field=field):
                def malformed_snapshot(durable, principal, *, _field=field):
                    payload = dict(self._snapshot(durable, principal))
                    payload[_field] = int(str(payload[_field]))
                    return payload

                app = self._application(
                    origin=self.origin,
                    boundary=self.boundary,
                    session=self.owner,
                    path=str(
                        Path(self.directory.name)
                        / f"snapshot-{field}.sqlite3"
                    ),
                    snapshot_provider=malformed_snapshot,
                )
                response = app.dispatch(
                    method="GET",
                    target="/api/v1/state",
                    headers=self.headers(),
                )
                self.assertEqual(response.status, 400)
                self.assertEqual(
                    self.body(response),
                    {"error": "INVALID_REQUEST"},
                )

    def test_command_expected_state_version_rejects_noncanonical_sequence(self):
        for version in ("00", "01", "+0", "-0", " 0", "0 ", "\u0660"):
            with self.subTest(version=version):
                command = self.command(expected_state_version=version)
                response = self.app.dispatch(
                    method="POST",
                    target="/api/v1/commands",
                    headers=self.headers(json_body=True),
                    body=json.dumps(command).encode("utf-8"),
                )
                self.assertEqual(response.status, 400)
                self.assertEqual(self.body(response), {"error": "INVALID_REQUEST"})
                self.assertEqual(self.app.store.state_version, 0)

    def test_event_cursor_rejects_noncanonical_query_aliases(self):
        targets = (
            "/api/v1/events?after=01",
            "/api/v1/events?after=%31",
            "/api/v1/events?after=%30",
            "/api/v1/events?a%66ter=0",
            "/api/v1/events?after=%2B1",
            "/api/v1/events?after=-0",
            "/api/v1/events?after=%200",
            "/api/v1/events?after=0%20",
            "/api/v1/events?after=",
        )
        for target in targets:
            with self.subTest(target=target):
                response = self.app.dispatch(
                    method="GET",
                    target=target,
                    headers=self.headers(),
                )
                self.assertEqual(response.status, 400)
                self.assertEqual(
                    self.body(response),
                    {"error": "INVALID_EVENT_CURSOR"},
                )

        canonical = self.app.dispatch(
            method="GET",
            target="/api/v1/events?after=0",
            headers=self.headers(),
        )
        self.assertEqual(canonical.status, 200)


    def test_canonical_long_state_version_returns_conflict_not_parse_error(self):
        command = self.command(expected_state_version="9" * 5000)
        response = self.app.dispatch(
            method="POST",
            target="/api/v1/commands",
            headers=self.headers(json_body=True),
            body=json.dumps(command).encode("utf-8"),
        )
        self.assertEqual(response.status, 409)
        payload = self.body(response)
        self.assertEqual(payload["status"], "CONFLICT")
        self.assertEqual(payload["reason_codes"], ["stale_state_version"])
        self.assertEqual(self.app.store.state_version, 0)

    def test_ui_command_contract_rejects_unknown_or_missing_top_level_fields(self):
        extra = self.command(unexpected="forbidden")
        response = self.post(extra)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.app.store.state_version, 0)

        missing = self.command()
        missing.pop("payload")
        response = self.post(missing)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.app.store.state_version, 0)

    def test_ui_command_contract_rejects_noncanonical_identity_and_sequence(self):
        canonical_uuid = "11111111-1111-1111-1111-111111111111"
        invalid_commands = (
            self.command(command_id="not-a-uuid"),
            self.command(command_id="{" + canonical_uuid + "}"),
            self.command(command_id="urn:uuid:" + canonical_uuid),
            self.command(command_id=canonical_uuid.replace("-", "")),
            self.command(expected_state_version="00"),
            self.command(expected_state_version="+0"),
            self.command(expected_state_version=" 0"),
            self.command(idempotency_key="x" * 129),
            self.command(session="sid-" + "A" * 64),
            self.command(environment="paper"),
            self.command(action="UNKNOWN_FUTURE_ACTION"),
            self.command(payload=[]),
        )
        for command in invalid_commands:
            with self.subTest(command=command):
                response = self.post(command)
                self.assertEqual(response.status, 400)
                self.assertEqual(self.app.store.state_version, 0)

    def test_ui_command_account_id_scope_is_not_whitespace_aliased(self):
        response = self.post(
            self.command(account_id=" paper-account-1 ")
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.app.store.state_version, 0)
        self.assertEqual(JournalStore(self.path).current_journal_sequence(), 0)

    def test_ui_command_distinct_schema_valid_idempotency_keys_do_not_alias(self):
        first = self.command(idempotency_key="host-network-key-distinct")
        first_response = self.post(first)
        self.assertEqual(first_response.status, 200)
        self.assertEqual(self.body(first_response)["status"], "ACCEPTED")
        self.assertEqual(self.app.store.state_version, 1)

        second = self.command(
            command_id="22222222-2222-4222-8222-222222222222",
            expected_state_version="1",
            idempotency_key=" host-network-key-distinct ",
        )
        second_response = self.post(second)
        self.assertEqual(second_response.status, 200)
        self.assertEqual(self.body(second_response)["status"], "ACCEPTED")
        self.assertEqual(self.app.store.state_version, 2)

    def test_ui_command_contract_accepts_exact_v5_shape(self):
        command = self.command()
        response = self.post(command)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.body(response)["status"], "ACCEPTED")
        self.assertEqual(self.app.store.state_version, 1)


if __name__ == "__main__":
    unittest.main()
