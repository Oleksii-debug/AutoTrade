from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile
from io import BytesIO

from mvp.autotrade_mvp.historical_host import HistoricalHostController
from mvp.autotrade_mvp.host_network import (
    AuthenticatedHostApplication,
    header_principal_resolver,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class _Protector:
    def protect(self, plaintext, *, entropy):
        return sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext, *, entropy):
        prefix = sha256(entropy).digest()
        if not ciphertext.startswith(prefix):
            raise OSError("scope mismatch")
        return ciphertext[len(prefix):][::-1]


class _Process:
    next_pid = 4000

    def __init__(self, *_args, **_kwargs):
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.code = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 0

    def wait(self, timeout=None):
        del timeout
        return self.code

    def kill(self):
        self.code = -9


class HistoricalHostContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.origin = "http://127.0.0.1:8765"
        boundary = SecurityBoundary(
            allowed_origins={self.origin},
            credential_vault=ProtectedCredentialVault(root / "vault.json", protector=_Protector()),
            session_authorizer=lambda subject, role, origin: True,
            now=lambda: 1000.0,
        )
        self.owner = boundary.create_session(subject="owner", role="OWNER", origin=self.origin, ttl_seconds=600)
        self.operator = boundary.create_session(subject="operator", role="OPERATOR", origin=self.origin, ttl_seconds=600)
        self.app = AuthenticatedHostApplication(
            JournalStore(root / "journal.sqlite3"),
            security_boundary=boundary,
            account_id="zero-local", environment="SIMULATION", host_id="host-local-1",
            public_origin=self.origin, principal_resolver=header_principal_resolver,
            snapshot_provider=lambda durable, principal: {
                "state_version": durable["state_version"], "event_cursor": durable["event_cursor"],
                "server_time": "2026-10-18T12:00:00Z", "host_id": "host-local-1",
                "account_id": durable["account_id"], "environment": durable["environment"],
                "permission_summary": {"actor": principal.actor, "session": principal.session, "role": principal.role},
                "connection_freshness": {"host": "CURRENT"}, "portfolio": {}, "risk": {},
                "strategy": {}, "jobs": [], "reason_codes": [],
            },
        )
        self.controller = HistoricalHostController(
            data_root=root / "historical-data", workspace=root / "historical-workspace",
            process_factory=_Process, now=lambda: "2026-10-18T12:00:00Z",
        )
        self.app.attach_historical_operator(self.controller)

    def headers(self, session, actor, *, json_body=False):
        result = {"Authorization": "AutoTrade-Session " + session.token,
                  "X-AutoTrade-Actor": actor, "Accept": "application/json"}
        if json_body:
            result["Content-Type"] = "application/json"
        return result

    def request(self, method, target, session=None, actor="owner"):
        headers = {} if session is None else self.headers(session, actor, json_body=method == "POST")
        return self.app.dispatch(method=method, target=target, headers=headers,
                                 body=(b"{}" if method == "POST" else b""))

    def test_reads_are_authenticated_and_never_claim_provider_authority(self):
        self.assertEqual(self.request("GET", "/api/v1/historical/status").status, 403)
        response = self.request("GET", "/api/v1/historical/status", self.operator, "operator")
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "IDLE")
        self.assertFalse(payload["trading_authority"])
        self.assertEqual(payload["provider_order_route"], "UNAVAILABLE")
        self.assertEqual(self.request("GET", "/api/v1/historical/results", self.owner).status, 200)
        self.assertEqual(self.request("GET", "/api/v1/historical/trades", self.owner).status, 200)

    def test_owner_controls_one_recoverable_zero_process_and_export(self):
        denied = self.request("POST", "/api/v1/historical/start", self.operator, "operator")
        self.assertEqual(denied.status, 403)
        started = self.request("POST", "/api/v1/historical/start", self.owner)
        self.assertEqual(json.loads(started.body)["status"], "RUNNING")
        paused = self.request("POST", "/api/v1/historical/pause", self.owner)
        self.assertEqual(json.loads(paused.body)["status"], "PAUSED")
        resumed = self.request("POST", "/api/v1/historical/resume", self.owner)
        self.assertEqual(json.loads(resumed.body)["status"], "RUNNING")
        stopped = self.request("POST", "/api/v1/historical/stop", self.owner)
        self.assertEqual(json.loads(stopped.body)["status"], "STOPPED")
        exported = self.request("POST", "/api/v1/historical/export", self.owner)
        self.assertEqual(exported.status, 200)
        self.assertEqual(exported.content_type, "application/zip")
        with ZipFile(BytesIO(exported.body)) as archive:
            self.assertEqual(set(archive.namelist()), {"status.json", "results.json", "trades.json"})

    def test_unknown_payload_is_rejected_without_starting_process(self):
        response = self.app.dispatch(
            method="POST", target="/api/v1/historical/start",
            headers=self.headers(self.owner, "owner", json_body=True), body=b'{"live":true}',
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.controller.status()["status"], "IDLE")


if __name__ == "__main__":
    unittest.main()
