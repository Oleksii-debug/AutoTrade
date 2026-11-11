from pathlib import Path
import os
import unittest
from unittest.mock import patch

from tools import write_contracts_ci_evidence as contracts_evidence


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "contracts.yml"


class ContractsCiEvidenceTests(unittest.TestCase):
    def test_current_contract_input_versions_are_coherent(self):
        versions = contracts_evidence._input_versions()
        self.assertEqual(versions["contract_version"], "7.0.0")
        self.assertEqual(versions["openapi_version"], versions["contract_version"])
        self.assertEqual(
            versions["fixture_corpus_version"],
            versions["contract_version"],
        )
        self.assertEqual(versions["exact_numeric_package_version"], "0.0.2")
        self.assertEqual(
            versions["research_exact_numeric_dependency"],
            "autotrade-exact-numeric==0.0.2",
        )
        validators = versions["semantic_validators"]
        self.assertEqual(
            [item["id"] for item in validators],
            ["dataset-manifest-content-authority-v1"],
        )
        self.assertEqual(
            set(validators[0]["bindings"]),
            {"python", "csharp", "typescript"},
        )
        self.assertEqual(
            validators[0]["installed_bindings"],
            {"python": "autotrade_numeric/dataset_manifest.py"},
        )
        self.assertEqual(validators[0]["case_count"], 13)
        shape = versions["shape_conformance"]
        self.assertEqual(shape["scope"], "closed-object-shape-subset")
        self.assertEqual(set(shape["bindings"]), {"python", "csharp", "typescript"})
        self.assertGreater(shape["definition_count"], 0)
        self.assertGreater(shape["case_count"], 0)

    def test_contracts_workflow_executes_every_recorded_base_command(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for command in contracts_evidence.BASE_VERIFIED_COMMANDS:
            with self.subTest(command=command):
                self.assertIn(command, workflow)
        self.assertIn(
            "python tools/contract_version_guard.py --base-ref "
            "origin/${{ github.base_ref }}",
            workflow,
        )
        self.assertIn(
            "python tools/write_contracts_ci_evidence.py --output "
            "\"artifacts/contracts-${{ runner.os }}.json\"",
            workflow,
        )
        self.assertIn(
            "python tools/generate_contract_shape_bindings.py --check",
            contracts_evidence.BASE_VERIFIED_COMMANDS,
        )
        self.assertTrue(
            any(
                "contracts/fixtures/contract-shapes.corpus.json" in command
                for command in contracts_evidence.BASE_VERIFIED_COMMANDS
            )
        )
        self.assertIn(
            "node tests/Contracts.TypeScript/contract-shapes.test.cjs",
            contracts_evidence.BASE_VERIFIED_COMMANDS,
        )

    def test_contract_evidence_contains_versions_commands_and_limits(self):
        base = {
            "schema_version": "1.0.0",
            "source_sha": "a" * 40,
            "checked_out_sha": "a" * 40,
            "suite": "contracts",
            "command": "WP-01 canonical contract qualification",
            "result": "PASS",
            "runner_os": "Linux",
            "python_version": "3.12.10",
            "github": {
                "event_name": "pull_request",
                "run_id": "1",
                "run_attempt": "1",
                "workflow": "contracts",
            },
            "generated_at": "2026-10-06T00:00:00+00:00",
            "contains_secrets": False,
        }
        with patch.object(
            contracts_evidence,
            "build_evidence",
            return_value=base,
        ), patch.dict(os.environ, {"GITHUB_BASE_REF": "main"}, clear=False):
            evidence = contracts_evidence.build_contracts_evidence()

        self.assertEqual(evidence["input_versions"]["contract_version"], "7.0.0")
        self.assertIn(
            "python tools/contract_version_guard.py --base-ref origin/main",
            evidence["tested_commands"],
        )
        self.assertEqual(
            evidence["tested_commands"][: len(contracts_evidence.BASE_VERIFIED_COMMANDS)],
            list(contracts_evidence.BASE_VERIFIED_COMMANDS),
        )
        self.assertEqual(
            evidence["unresolved_limits"],
            list(contracts_evidence.UNRESOLVED_LIMITS),
        )
        self.assertTrue(evidence["unresolved_limits"])


if __name__ == "__main__":
    unittest.main()
