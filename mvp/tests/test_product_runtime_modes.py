from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.host_actions import required_roles_for_host_action
from mvp.autotrade_mvp.operator_authority_commands import (
    AuthorityExecutionResult,
    canonical_operator_payload,
    execute_operator_authority_action,
    validate_authority_success_evidence,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_modes import (
    RUNTIME_MODES,
    assert_order_submission_allowed,
    assert_runtime_action_allowed,
    current_runtime_mode,
    ensure_runtime_mode,
)


class ProductRuntimeModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.journal = JournalStore(self.root / "journal.sqlite3")
        ensure_runtime_mode(
            self.journal,
            account_id="simulation-account",
            environment="SIMULATION",
            transitioned_at="2026-10-20T00:00:00Z",
        )

    def tearDown(self):
        self.temp.cleanup()

    def transition(self, mode, ordinal):
        command_id = str(uuid4())
        payload = canonical_operator_payload(
            self.journal,
            "SET_RUNTIME_MODE",
            {"mode": mode},
            command_id,
            "simulation-account",
            "SIMULATION",
        )
        accepted_at = f"2026-10-20T00:00:{ordinal:02d}Z"
        result = execute_operator_authority_action(
            self.journal,
            "SET_RUNTIME_MODE",
            payload,
            payload_digest(payload),
            "simulation-account",
            "SIMULATION",
            accepted_at,
        )
        self.assertIsInstance(result, AuthorityExecutionResult)
        validate_authority_success_evidence(
            self.journal,
            "SET_RUNTIME_MODE",
            payload,
            payload_digest(payload),
            "simulation-account",
            "SIMULATION",
            accepted_at,
            result.affected_refs,
            result.evidence,
        )
        return payload, result

    def test_exact_five_modes_persist_with_preservation_checkpoint(self):
        self.assertEqual(
            RUNTIME_MODES,
            {"RUNNING", "PAUSED", "RESEARCH_ONLY", "RECOVERY", "STOPPED"},
        )
        self.assertEqual(current_runtime_mode(self.journal)["mode"], "RUNNING")
        for ordinal, mode in enumerate(
            ("RUNNING", "RESEARCH_ONLY", "PAUSED", "STOPPED", "RECOVERY"),
            start=1,
        ):
            self.transition(mode, ordinal)
            state = current_runtime_mode(self.journal)
            self.assertEqual(state["mode"], mode)
            checkpoint = state["checkpoint"]
            self.assertTrue(checkpoint["memory_preserved"])
            self.assertTrue(checkpoint["strategies_preserved"])
            self.assertTrue(checkpoint["diagnostics_preserved"])
            self.assertEqual(
                checkpoint["order_submission_allowed"], mode == "RUNNING"
            )
        reopened = JournalStore(self.root / "journal.sqlite3")
        self.assertEqual(current_runtime_mode(reopened)["mode"], "RECOVERY")

    def test_mode_gates_new_decisions_orders_and_recovery(self):
        assert_runtime_action_allowed(self.journal, "START_SIMULATION")
        assert_order_submission_allowed(self.journal)

        self.transition("PAUSED", 1)
        with self.assertRaisesRegex(ValueError, "RUNNING"):
            assert_runtime_action_allowed(self.journal, "START_SIMULATION")
        with self.assertRaisesRegex(PermissionError, "RUNNING"):
            assert_order_submission_allowed(self.journal)

        self.transition("RUNNING", 2)
        assert_runtime_action_allowed(self.journal, "START_SIMULATION")
        assert_order_submission_allowed(self.journal)
        # Recovery remains callable from any mode so an operator can repair a
        # damaged run without first admitting ordinary trading.
        assert_runtime_action_allowed(self.journal, "RECOVER_SIMULATION")

        self.transition("RESEARCH_ONLY", 3)
        with self.assertRaisesRegex(PermissionError, "RUNNING"):
            assert_order_submission_allowed(self.journal)
        with self.assertRaisesRegex(ValueError, "RUNNING"):
            assert_runtime_action_allowed(self.journal, "START_SIMULATION")

        self.transition("RECOVERY", 4)
        assert_runtime_action_allowed(self.journal, "RECOVER_SIMULATION")
        with self.assertRaisesRegex(ValueError, "reconcile"):
            self.transition("RUNNING", 5)

    def test_transition_is_idempotent_and_tamper_safe(self):
        payload, first = self.transition("STOPPED", 1)
        second = execute_operator_authority_action(
            self.journal,
            "SET_RUNTIME_MODE",
            payload,
            payload_digest(payload),
            "simulation-account",
            "SIMULATION",
            "2026-10-20T00:00:01Z",
        )
        self.assertEqual(second, first)
        changed = dict(payload, mode="RUNNING")
        with self.assertRaises(ValueError):
            execute_operator_authority_action(
                self.journal,
                "SET_RUNTIME_MODE",
                changed,
                payload_digest(changed),
                "simulation-account",
                "SIMULATION",
                "2026-10-20T00:00:01Z",
            )

    def test_host_role_and_semantic_ui_expose_mode_control(self):
        self.assertEqual(
            required_roles_for_host_action("SET_RUNTIME_MODE"),
            {"OWNER", "OPERATOR"},
        )
        root = Path(__file__).resolve().parents[2]
        html = (root / "web" / "src" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "src" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="runtime-mode"', html)
        self.assertIn('value="RESEARCH_ONLY"', html)
        self.assertIn('SET_RUNTIME_MODE: new Set(["OWNER", "OPERATOR"])', script)
        self.assertIn('return Object.freeze({mode});', script)


if __name__ == "__main__":
    unittest.main()
