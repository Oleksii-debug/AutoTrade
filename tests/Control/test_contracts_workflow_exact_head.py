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
        self.assertIn("python tools/write_ci_evidence.py --suite contracts", text)
        self.assertIn("contracts-evidence-${{ runner.os }}", text)
        self.assertIn("if-no-files-found: error", text)


if __name__ == "__main__":
    unittest.main()
