import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "contracts.yml"


class ContractsWorkflowExactHeadTests(unittest.TestCase):
    def test_contracts_workflow_checks_out_exact_source_sha(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "AUTOTRADE_SOURCE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}",
            text,
        )
        self.assertIn(
            "AUTOTRADE_PR_HEAD_SHA: ${{ github.event.pull_request.head.sha || '' }}",
            text,
        )
        self.assertIn("ref: ${{ env.AUTOTRADE_SOURCE_SHA }}", text)

    def test_contracts_workflow_publishes_exact_head_evidence(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        # The dedicated evidence writer records the exact executed commands,
        # input versions, and PR/non-PR scope; a generic placeholder would not.
        self.assertEqual(
            text.count("python tools/write_contracts_ci_evidence.py --output"),
            2,
        )
        self.assertIn(
            'python tools/write_contracts_ci_evidence.py --output "artifacts/contracts-${{ runner.os }}.json"',
            text,
        )
        self.assertIn("if: github.event_name == 'pull_request'", text)
        self.assertIn("if: github.event_name != 'pull_request'", text)
        writer = (ROOT / "tools" / "write_contracts_ci_evidence.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def build_contracts_evidence()", writer)
        self.assertIn('"tested_commands"] = verified_commands(event_name)', writer)
        self.assertIn('"input_versions"] = _input_versions()', writer)
        self.assertIn('"unresolved_limits"] = list(UNRESOLVED_LIMITS)', writer)
        self.assertIn('if event_name == "pull_request":', writer)
        self.assertIn('"GITHUB_BASE_REF"', writer)
        self.assertIn("contracts-evidence-${{ runner.os }}", text)
        self.assertIn("if-no-files-found: error", text)


if __name__ == "__main__":
    unittest.main()
