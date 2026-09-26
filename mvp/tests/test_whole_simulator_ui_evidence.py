from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accessibility import format_accessible_status
from mvp.autotrade_mvp.cli import get_status
from mvp.autotrade_mvp.durable_host_api import JournalBackedHostCommandStore
from mvp.autotrade_mvp.economics import build_economic_report
from mvp.autotrade_mvp.host_network import (
    AuthenticatedHostApplication,
    header_principal_resolver,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.pipeline import run_multi_episode, run_vertical_slice
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.web_surface import render_semantic_page
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


ROOT = Path(__file__).resolve().parents[2]
SHIPPED_WEB_APP = ROOT / "web" / "src" / "app.js"


class _DeterministicProtector:
    PREFIX = b"wp55-host-network-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]



def _execute_shipped_web_snapshot(snapshot):
    """Execute the shipped browser parser/renderer against one canonical snapshot."""

    node = shutil.which("node")
    if node is None:
        if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
            raise AssertionError(
                "GitHub qualification requires Node to execute shipped web semantics"
            )
        raise unittest.SkipTest(
            "Node is unavailable; shipped web semantics require a JavaScript runtime"
        )

    harness = r"""
const fs = require("fs");
const vm = require("vm");

const appPath = process.argv[1];
const marker = '  document.addEventListener("DOMContentLoaded", start);\n})();';
let source = fs.readFileSync(appPath, "utf8");
// Git checkout may materialize app.js as CRLF on Windows. Normalize only the
// in-memory test copy so the shipped source itself remains untouched.
source = source.replace(/\r\n/g, "\n");
if (!source.includes(marker)) {
  throw new Error("shipped app.js test-export marker not found");
}
source = source.replace(
  marker,
  '  window.__AUTOTRADE_TEST__ = { parseCanonicalSnapshot, renderSnapshot };\n' +
    marker
);

class Element {
  constructor(id = "") {
    this.id = id;
    this.textContent = "";
    this.value = "";
    this.disabled = false;
    this.children = [];
    this.dataset = {};
    this.options = [];
    this.scope = "";
    this.colSpan = 0;
    this.firstElementChild = null;
  }
  _syncFirst() {
    this.firstElementChild = this.children.length > 0 ? this.children[0] : null;
  }
  replaceChildren(...children) {
    this.children = [...children];
    this._syncFirst();
  }
  append(...children) {
    this.children.push(...children);
    this._syncFirst();
  }
  appendChild(child) {
    this.children.push(child);
    this._syncFirst();
    return child;
  }
  prepend(child) {
    this.children.unshift(child);
    this._syncFirst();
  }
  querySelector(selector) {
    if (this.id === "host-command-form" && selector === 'button[type="submit"]') {
      return elements.get("submit-command");
    }
    return null;
  }
  querySelectorAll() {
    return [];
  }
  focus() {}
  addEventListener() {}
  remove() {}
}

const ids = [
  "state-version",
  "event-cursor",
  "expected-state-version",
  "active-host",
  "active-account",
  "active-environment",
  "connection-summary",
  "freshness",
  "server-time",
  "permissions-body",
  "portfolio-body",
  "risk-body",
  "strategy-body",
  "jobs-body",
  "host-command-form",
  "host-action",
  "submit-command",
  "command-result",
  "notification-history",
  "polite-status",
  "urgent-status"
];
const elements = new Map(ids.map((id) => [id, new Element(id)]));
const action = elements.get("host-action");
action.options = [
  {value: "BLOCK_NEW_EXPOSURE", disabled: false},
  {value: "REVOKE_AUTHORITY", disabled: false}
];
action.value = "BLOCK_NEW_EXPOSURE";

global.document = {
  activeElement: null,
  getElementById(id) {
    return elements.get(id) || null;
  },
  createElement() {
    return new Element();
  },
  addEventListener() {}
};
global.window = {
  addEventListener() {},
  setTimeout(callback) {
    callback();
    return 1;
  },
  setInterval() {
    return 1;
  }
};
global.fetch = async function () {
  throw new Error("test harness must not perform network I/O");
};

vm.runInThisContext(source, {filename: appPath});
const api = window.__AUTOTRADE_TEST__;
if (!api) throw new Error("shipped app.js test export was not installed");

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", () => {
  const snapshot = JSON.parse(input);
  const parsed = api.parseCanonicalSnapshot(snapshot);
  api.renderSnapshot(snapshot);

  function table(id) {
    return elements.get(id).children.map(
      (row) => row.children.map((cell) => cell.textContent)
    );
  }

  process.stdout.write(JSON.stringify({
    parsed: {
      version: parsed.version.toString(),
      cursor: parsed.cursor.toString(),
      hostId: parsed.hostId,
      accountId: parsed.accountId,
      environment: parsed.environment
    },
    stateVersion: elements.get("state-version").textContent,
    eventCursor: elements.get("event-cursor").textContent,
    activeHost: elements.get("active-host").textContent,
    activeAccount: elements.get("active-account").textContent,
    activeEnvironment: elements.get("active-environment").textContent,
    portfolio: table("portfolio-body"),
    risk: table("risk-body"),
    strategy: table("strategy-body"),
    jobs: table("jobs-body")
  }));
});
"""

    completed = subprocess.run(
        [node, "-e", harness, str(SHIPPED_WEB_APP)],
        input=json.dumps(snapshot, sort_keys=True),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "shipped web parser/renderer failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    return json.loads(completed.stdout)


class WholeSimulatorUiEvidenceTests(unittest.TestCase):
    def test_durable_simulator_state_reaches_accessible_text_and_semantic_web(self):
        with TemporaryDirectory() as directory:
            episodes = (
                ("100", "101", "102", "103"),
                ("103", "102", "101", "100"),
            )
            results = run_multi_episode(episodes, directory)

            self.assertEqual([item.status for item in results], ["filled", "filled"])
            self.assertEqual(results[-1].position, 0)
            self.assertTrue(results[-1].reconciled)

            status = get_status(directory)
            self.assertEqual(status["status"], "running")
            self.assertTrue(status["replay_verified"])
            self.assertEqual(status["evidence_count"], 2)
            self.assertEqual(len(status["fills"]), 2)

            report = build_economic_report(directory).as_jsonable()
            self.assertTrue(report["reconciled"])
            self.assertEqual(report["economic_edge_claim"], "UNPROVEN_SIMULATION_ONLY")

            accessible = format_accessible_status(status, report)
            self.assertIn("Mode: simulation only", accessible)
            self.assertIn("Live order submission: unavailable", accessible)
            self.assertIn("Replay verification: passed", accessible)
            self.assertIn("Recorded evidence items: 2", accessible)
            self.assertIn("Recorded fills: 2", accessible)
            self.assertIn(f"Final equity: {report['final_equity']}", accessible)
            self.assertIn("Economic reconciliation: passed", accessible)
            self.assertIn("Economic edge: unproven", accessible)

            # The web renderer consumes the same durable status text. Host command
            # state is intentionally empty here: simulator financial completion is
            # not fabricated into a host-operation completion claim.
            html = render_semantic_page(
                {
                    "state_version": "0",
                    "event_cursor": "0",
                    "operations": {},
                },
                status_text=accessible,
                announcement="Simulation state restored and replay verification passed.",
            )

            self.assertIn("<h2 id=\"status-heading\">System status</h2>", html)
            self.assertIn("Mode: simulation only", html)
            self.assertIn("Live order submission: unavailable", html)
            self.assertIn("Replay verification: passed", html)
            self.assertIn(f"Final equity: {report['final_equity']}", html)
            self.assertIn("Economic reconciliation: passed", html)
            self.assertIn("Economic edge: unproven", html)
            self.assertIn("No operations are currently recorded.", html)
            self.assertNotIn("economic edge: proven", html.lower())
            self.assertNotIn("live order submission: available", html.lower())

    def test_restarted_simulator_state_flows_through_host_snapshot_to_shipped_web_contract(self):
        with TemporaryDirectory() as directory:
            episodes = (
                ("100", "101", "102", "103"),
                ("103", "102", "101", "100"),
            )
            run_multi_episode(episodes, directory)
            restarted = run_multi_episode(episodes, directory)
            self.assertTrue(all(item.resumed for item in restarted))

            status = get_status(directory)
            report = build_economic_report(directory).as_jsonable()
            self.assertTrue(status["replay_verified"])
            self.assertTrue(report["reconciled"])

            origin = "http://127.0.0.1:8765"
            vault = ProtectedCredentialVault(
                Path(directory) / "host-credentials.json",
                protector=_DeterministicProtector(),
            )
            boundary = SecurityBoundary(
                allowed_origins={origin},
                credential_vault=vault,
                session_authorizer=lambda subject, role, paired_origin: (
                    subject == "owner"
                    and role == "OWNER"
                    and paired_origin == origin
                ),
                now=lambda: 1000.0,
            )
            owner = boundary.create_session(
                subject="owner",
                role="OWNER",
                origin=origin,
                ttl_seconds=600,
            )

            def snapshot_provider(durable, principal):
                return {
                    "state_version": durable["state_version"],
                    "event_cursor": durable["event_cursor"],
                    "server_time": "2026-09-26T00:00:00Z",
                    "host_id": "host-simulator-1",
                    "account_id": durable["account_id"],
                    "environment": durable["environment"],
                    "permission_summary": {
                        "actor": principal.actor,
                        "session": principal.session,
                        "role": principal.role,
                    },
                    "connection_freshness": {
                        "host": "CURRENT",
                        "as_of": "2026-09-26T00:00:00Z",
                    },
                    "portfolio": {
                        "position": str(restarted[-1].position),
                        "fill_count": len(status["fills"]),
                        "final_equity": report["final_equity"],
                        "economic_reconciled": report["reconciled"],
                    },
                    "risk": {
                        "live_order_submission": "UNAVAILABLE",
                        "replay_verified": status["replay_verified"],
                        "economic_edge_claim": report["economic_edge_claim"],
                    },
                    "strategy": {
                        "simulator_status": status["status"],
                        "evidence_count": status["evidence_count"],
                        "resumed_from_durable_state": True,
                    },
                    "jobs": [],
                    "reason_codes": [],
                }

            app = AuthenticatedHostApplication(
                JournalStore(str(Path(directory) / "host-journal.sqlite3")),
                security_boundary=boundary,
                account_id="sim-account",
                environment="SIMULATION",
                host_id="host-simulator-1",
                public_origin=origin,
                principal_resolver=header_principal_resolver,
                snapshot_provider=snapshot_provider,
                now=lambda: "2026-09-26T00:00:00Z",
            )
            response = app.dispatch(
                method="GET",
                target="/api/v1/state",
                headers={
                    "Authorization": "AutoTrade-Session " + owner.token,
                    "X-AutoTrade-Actor": "owner",
                    "Accept": "application/json",
                },
            )
            self.assertEqual(response.status, 200)
            snapshot = json.loads(response.body.decode("utf-8"))

            self.assertEqual(snapshot["environment"], "SIMULATION")
            self.assertEqual(snapshot["account_id"], "sim-account")
            self.assertEqual(
                snapshot["portfolio"]["position"],
                str(restarted[-1].position),
            )
            self.assertEqual(restarted[-1].position, 0)
            self.assertEqual(snapshot["portfolio"]["fill_count"], 2)
            self.assertEqual(
                snapshot["portfolio"]["final_equity"],
                report["final_equity"],
            )
            self.assertTrue(snapshot["portfolio"]["economic_reconciled"])
            self.assertTrue(snapshot["risk"]["replay_verified"])
            self.assertEqual(
                snapshot["risk"]["economic_edge_claim"],
                "UNPROVEN_SIMULATION_ONLY",
            )
            self.assertEqual(snapshot["risk"]["live_order_submission"], "UNAVAILABLE")
            self.assertEqual(snapshot["strategy"]["evidence_count"], 2)
            self.assertTrue(snapshot["strategy"]["resumed_from_durable_state"])
            self.assertNotIn(owner.token, response.body.decode("utf-8"))

            rendered = _execute_shipped_web_snapshot(snapshot)
            self.assertEqual(rendered["parsed"]["version"], snapshot["state_version"])
            self.assertEqual(rendered["parsed"]["cursor"], snapshot["event_cursor"])
            self.assertEqual(rendered["parsed"]["hostId"], "host-simulator-1")
            self.assertEqual(rendered["parsed"]["accountId"], "sim-account")
            self.assertEqual(rendered["parsed"]["environment"], "SIMULATION")
            self.assertEqual(rendered["stateVersion"], snapshot["state_version"])
            self.assertEqual(rendered["eventCursor"], snapshot["event_cursor"])
            self.assertEqual(rendered["activeHost"], "host-simulator-1")
            self.assertEqual(rendered["activeAccount"], "sim-account")
            self.assertEqual(rendered["activeEnvironment"], "SIMULATION")

            portfolio = dict(rendered["portfolio"])
            risk = dict(rendered["risk"])
            strategy = dict(rendered["strategy"])
            self.assertEqual(portfolio["position"], str(restarted[-1].position))
            self.assertEqual(portfolio["fill_count"], "2")
            self.assertEqual(
                portfolio["final_equity"],
                str(report["final_equity"]),
            )
            self.assertEqual(portfolio["economic_reconciled"], "true")
            self.assertEqual(risk["live_order_submission"], "UNAVAILABLE")
            self.assertEqual(risk["replay_verified"], "true")
            self.assertEqual(
                risk["economic_edge_claim"],
                "UNPROVEN_SIMULATION_ONLY",
            )
            self.assertEqual(strategy["simulator_status"], status["status"])
            self.assertEqual(strategy["evidence_count"], "2")
            self.assertEqual(strategy["resumed_from_durable_state"], "true")
            self.assertEqual(
                rendered["jobs"],
                [["No background jobs reported by the host snapshot."]],
            )

    def test_restart_preserves_accessible_economic_facts_without_duplicate_evidence(self):
        with TemporaryDirectory() as directory:
            episodes = (
                ("100", "101", "102", "103"),
                ("103", "102", "101", "100"),
            )
            first = run_multi_episode(episodes, directory)
            first_status = get_status(directory)
            first_report = build_economic_report(directory).as_jsonable()
            first_text = format_accessible_status(first_status, first_report)

            restarted = run_multi_episode(episodes, directory)
            restarted_status = get_status(directory)
            restarted_report = build_economic_report(directory).as_jsonable()
            restarted_text = format_accessible_status(
                restarted_status,
                restarted_report,
            )

            self.assertTrue(all(item.resumed for item in restarted))
            self.assertEqual(restarted[-1].position, first[-1].position)
            self.assertEqual(restarted_status["evidence_count"], 2)
            self.assertEqual(len(restarted_status["fills"]), 2)
            self.assertEqual(restarted_report, first_report)
            self.assertEqual(restarted_text, first_text)

    def test_no_trade_path_reaches_ui_without_fabricated_execution(self):
        with TemporaryDirectory() as directory:
            result = run_vertical_slice(("100", "100", "100"), directory)
            self.assertEqual(result.status, "hold")
            self.assertIsNone(result.order_id)
            self.assertIsNone(result.fill_id)

            status = get_status(directory)
            report = build_economic_report(directory).as_jsonable()
            accessible = format_accessible_status(status, report)
            html = render_semantic_page(
                {"state_version": "0", "event_cursor": "0", "operations": {}},
                status_text=accessible,
            )

            self.assertEqual(status["evidence_count"], 1)
            self.assertEqual(status["fills"], {})
            self.assertEqual(report["trade_count"], 0)
            self.assertIn("Recorded fills: 0", accessible)
            self.assertIn("Economic reconciliation: passed", accessible)
            self.assertIn("Recorded fills: 0", html)
            self.assertNotIn("<strong>SUCCEEDED</strong>", html)

    def test_tampered_replay_surfaces_recovery_instead_of_healthy_state(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice(("100", "101", "102", "103"), directory)

            evidence_path = Path(directory) / "learning-evidence.jsonl"
            rows = [
                json.loads(line)
                for line in evidence_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            rows[0]["reconciled"] = False
            evidence_path.write_text(
                "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
                encoding="utf-8",
            )

            status = get_status(directory)
            self.assertEqual(status["status"], "needs_recovery")
            self.assertFalse(status["replay_verified"])

            accessible = format_accessible_status(status)
            html = render_semantic_page(
                {"state_version": "0", "event_cursor": "0", "operations": {}},
                status_text=accessible,
                announcement="Recovery required before trusting current state.",
            )

            self.assertIn("System state: Needs recovery", accessible)
            self.assertIn("Replay verification: failed", accessible)
            self.assertIn("recovery or reconciliation is needed", accessible)
            self.assertNotIn("Economic reconciliation: passed", accessible)
            self.assertIn("System state: Needs recovery", html)
            self.assertIn("Replay verification: failed", html)
            self.assertNotIn("Economic reconciliation: passed", html)


    def test_risk_rejection_reaches_ui_with_zero_financial_execution(self):
        with TemporaryDirectory() as directory:
            result = run_vertical_slice(
                ("100", "101", "102", "103"),
                directory,
                max_notional="10",
            )
            self.assertEqual(result.status, "risk_rejected")
            self.assertIsNone(result.order_id)
            self.assertIsNone(result.fill_id)

            status = get_status(directory)
            report = build_economic_report(directory).as_jsonable()
            accessible = format_accessible_status(status, report)
            html = render_semantic_page(
                {"state_version": "0", "event_cursor": "0", "operations": {}},
                status_text=accessible,
            )

            self.assertEqual(status["status"], "running")
            self.assertTrue(status["replay_verified"])
            self.assertEqual(status["fills"], {})
            self.assertEqual(report["trade_count"], 0)
            self.assertIn("Recorded fills: 0", accessible)
            self.assertIn("Economic reconciliation: passed", accessible)
            self.assertIn("Recorded fills: 0", html)
            self.assertNotIn("<strong>SUCCEEDED</strong>", html)

    def test_corrupt_checkpoint_is_fail_closed_on_accessible_ui(self):
        with TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            checkpoint.write_text('{"schema_version": 999}', encoding="utf-8")

            status = get_status(directory)
            self.assertEqual(status["status"], "needs_recovery")
            self.assertFalse(status["replay_verified"])
            self.assertEqual(status["evidence_count"], 0)
            self.assertEqual(status["fills"], {})

            accessible = format_accessible_status(status)
            html = render_semantic_page(
                {"state_version": "0", "event_cursor": "0", "operations": {}},
                status_text=accessible,
                announcement="State cannot be trusted until inspected or restored.",
            )

            self.assertIn("System state: Needs recovery", accessible)
            self.assertIn("Replay verification: failed", accessible)
            self.assertIn("recovery or reconciliation is needed", accessible)
            self.assertIn("Economic edge: unproven", accessible)
            self.assertIn("System state: Needs recovery", html)
            self.assertNotIn("Economic reconciliation: passed", html)


    def test_unknown_durable_operation_remains_unknown_after_restart_and_in_ui(self):
        with TemporaryDirectory() as directory:
            journal_path = f"{directory}/host.sqlite3"

            def host():
                return JournalBackedHostCommandStore(
                    JournalStore(journal_path),
                    account_id="sim-account",
                    environment="SIMULATION",
                    session_validator=lambda session, actor, origin, action: (
                        session == "session-a"
                        and actor == "operator"
                        and origin == "https://local.autotrade.invalid"
                    ),
                    request_origin_provider=lambda: "https://local.autotrade.invalid",
                    now=lambda: "2026-09-24T18:00:00Z",
                )

            first = host()
            accepted = first.submit(
                {
                    "command_id": "11111111-1111-1111-1111-111111111111",
                    "expected_state_version": "0",
                    "idempotency_key": "wp55-unknown-1",
                    "actor": "operator",
                    "session": "session-a",
                    "account_id": "sim-account",
                    "environment": "SIMULATION",
                    "action": "BLOCK_NEW_EXPOSURE",
                    "payload": {},
                }
            )
            self.assertEqual(accepted.status, "ACCEPTED")
            unknown = first.update_operation(
                accepted.operation_id,
                "UNKNOWN",
                remaining_uncertainty=("provider_outcome_unresolved",),
            )
            self.assertEqual(unknown.phase, "UNKNOWN")

            restarted = host()
            projected = restarted.get_operation(accepted.operation_id)
            self.assertEqual(projected.phase, "UNKNOWN")
            self.assertEqual(
                projected.remaining_uncertainty,
                ("provider_outcome_unresolved",),
            )

            html = render_semantic_page(
                restarted.snapshot(),
                status_text=(
                    "AutoTrade status\n"
                    "Mode: simulation only\n"
                    "Live order submission: unavailable\n"
                    "Economic edge: unproven"
                ),
                announcement="Operation outcome remains unknown and requires reconciliation.",
            )
            self.assertIn("<strong>UNKNOWN</strong>", html)
            self.assertIn("outcome remains unknown", html)
            self.assertNotIn("<strong>SUCCEEDED</strong>", html)


    def test_accepted_durable_command_stays_queued_after_restart_and_in_ui(self):
        with TemporaryDirectory() as directory:
            journal_path = f"{directory}/host.sqlite3"

            def host():
                return JournalBackedHostCommandStore(
                    JournalStore(journal_path),
                    account_id="sim-account",
                    environment="SIMULATION",
                    session_validator=lambda session, actor, origin, action: (
                        session == "session-b"
                        and actor == "operator"
                        and origin == "https://local.autotrade.invalid"
                    ),
                    request_origin_provider=lambda: "https://local.autotrade.invalid",
                    now=lambda: "2026-09-24T18:00:00Z",
                )

            first = host()
            accepted = first.submit(
                {
                    "command_id": "22222222-2222-2222-2222-222222222222",
                    "expected_state_version": "0",
                    "idempotency_key": "wp55-queued-1",
                    "actor": "operator",
                    "session": "session-b",
                    "account_id": "sim-account",
                    "environment": "SIMULATION",
                    "action": "BLOCK_NEW_EXPOSURE",
                    "payload": {},
                }
            )
            self.assertEqual(accepted.status, "ACCEPTED")

            restarted = host()
            operation = restarted.get_operation(accepted.operation_id)
            self.assertEqual(operation.phase, "QUEUED")
            self.assertIn(
                "financial_outcome_not_completed",
                operation.remaining_uncertainty,
            )

            html = render_semantic_page(
                restarted.snapshot(),
                status_text=(
                    "AutoTrade status\n"
                    "Mode: simulation only\n"
                    "Live order submission: unavailable\n"
                    "Economic edge: unproven"
                ),
                announcement=(
                    "Command accepted for processing; financial completion "
                    "has not been established."
                ),
            )
            self.assertIn("<strong>QUEUED</strong>", html)
            self.assertIn("financial completion has not been established", html)
            self.assertNotIn("<strong>SUCCEEDED</strong>", html)


if __name__ == "__main__":
    unittest.main()
