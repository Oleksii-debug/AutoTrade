from pathlib import Path
import unittest

from tools.check_dependency_composition import (
    audit_composition,
    is_exact_python_requirement,
    qualification_exit_code,
)


class DependencyCompositionGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = audit_composition()

    def test_current_tree_is_not_falsely_declared_release_qualified(self):
        self.assertFalse(self.report.qualified)
        self.assertTrue(self.report.blockers)

    def test_report_mode_stays_usable_while_strict_release_mode_fails_closed(self):
        self.assertEqual(
            qualification_exit_code(self.report, require_qualified=False),
            0,
        )
        self.assertEqual(
            qualification_exit_code(self.report, require_qualified=True),
            1,
        )

    def test_runtime_python_requirements_are_exact(self):
        self.assertGreater(len(self.report.exact_python_requirements), 0)
        self.assertTrue(
            all("==" in item for item in self.report.exact_python_requirements)
        )
        self.assertFalse(
            any(
                blocker.startswith("NON_EXACT_PYTHON_REQUIREMENT:")
                for blocker in self.report.blockers
            )
        )

    def test_exact_python_pin_rejects_wildcards_markers_and_ranges(self):
        self.assertTrue(is_exact_python_requirement("attrs==26.1.0"))
        for value in (
            "attrs==26.*",
            "attrs==26.1.0;python_version>='3.12'",
            "attrs==26.1.0,!=26.1.1",
            "attrs>=26.1.0",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_exact_python_requirement(value))

    def test_research_build_dependency_is_exact(self):
        self.assertNotIn("MISSING_RESEARCH_BUILD_REQUIREMENTS", self.report.blockers)
        self.assertFalse(
            any(
                blocker.startswith("NON_EXACT_RESEARCH_BUILD_REQUIREMENT:")
                for blocker in self.report.blockers
            )
        )
        pyproject = (
            Path(__file__).resolve().parents[2] / "research" / "pyproject.toml"
        ).read_text(encoding="utf-8")
        self.assertIn('requires = ["setuptools==84.0.0"]', pyproject)

    def test_dotnet_sdk_is_exact_and_roll_forward_is_disabled(self):
        self.assertEqual(self.report.dotnet_sdk, "10.0.100")
        self.assertFalse(
            any(
                blocker.startswith("DOTNET_ROLL_FORWARD_NOT_DISABLED:")
                for blocker in self.report.blockers
            )
        )

    def test_dotnet_ci_installs_the_same_exact_sdk(self):
        root = Path(__file__).resolve().parents[2]
        foundation = (root / ".github" / "workflows" / "dotnet-foundation.yml").read_text(
            encoding="utf-8"
        )
        lean = (root / ".github" / "workflows" / "lean-adoption.yml").read_text(
            encoding="utf-8"
        )
        contracts = (root / ".github" / "workflows" / "contracts.yml").read_text(
            encoding="utf-8"
        )
        verify = (root / ".github" / "workflows" / "verify.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(foundation.count('dotnet-version: "10.0.100"'), 2)
        self.assertEqual(lean.count('dotnet-version: "10.0.100"'), 1)
        self.assertEqual(contracts.count('dotnet-version: "10.0.100"'), 1)
        self.assertEqual(verify.count('dotnet-version: "10.0.100"'), 1)
        for workflow in (foundation, lean, contracts, verify):
            self.assertNotIn('dotnet-version: "10.0.x"', workflow)

    def test_ci_python_runtime_is_exact(self):
        blockers = {
            item
            for item in self.report.blockers
            if item.startswith("NON_EXACT_CI_PYTHON_VERSION:")
        }
        self.assertEqual(blockers, set())

    def test_unresolved_first_party_rights_remain_fail_closed(self):
        unresolved = {
            blocker
            for blocker in self.report.blockers
            if blocker.startswith("UNRESOLVED_COMPONENT_RIGHTS:")
        }
        self.assertEqual(
            unresolved,
            {
                "UNRESOLVED_COMPONENT_RIGHTS:Autosport first-party source",
                "UNRESOLVED_COMPONENT_RIGHTS:Nika Core first-party source",
            },
        )

    def test_pending_external_composition_is_not_release_approved(self):
        pending = {
            blocker
            for blocker in self.report.blockers
            if blocker.startswith("UNQUALIFIED_SOURCE_COMPOSITION:")
        }
        self.assertIn(
            "UNQUALIFIED_SOURCE_COMPOSITION:QuantConnect LEAN",
            pending,
        )
        self.assertIn(
            "UNQUALIFIED_SOURCE_COMPOSITION:WhiteBit.Net",
            pending,
        )
        self.assertIn(
            "UNQUALIFIED_SOURCE_COMPOSITION:CryptoExchange.Net",
            pending,
        )
        self.assertIn(
            "UNQUALIFIED_SOURCE_COMPOSITION:Alpaca official C# SDK",
            pending,
        )


if __name__ == "__main__":
    unittest.main()
