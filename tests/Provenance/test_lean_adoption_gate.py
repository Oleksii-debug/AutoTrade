import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
PIN_PATH = ROOT / "src" / "AutoTrade.Engine.Lean" / "lean.pin.json"
EVIDENCE_PATH = (
    ROOT / "src" / "AutoTrade.Engine.Lean" / "lean.adoption.evidence.json"
)
COMPONENTS_PATH = ROOT / "provenance" / "components.json"


class LeanAdoptionProvenanceTests(unittest.TestCase):
    def load(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def lean_component(self):
        components = self.load(COMPONENTS_PATH)["components"]
        matches = [
            item for item in components
            if item["name"] == "QuantConnect LEAN"
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_wp02_pin_matches_canonical_provenance_identity(self):
        pin = self.load(PIN_PATH)
        component = self.lean_component()
        self.assertEqual(pin["repository"], component["repository"])
        self.assertEqual(pin["commit"], component["revision"])
        self.assertEqual(pin["tree"], component["tree"])
        self.assertEqual(pin["license"], component["license"])
        self.assertEqual(
            pin["license_blob_sha"],
            component["license_blob_sha"],
        )
        self.assertEqual(
            pin["source_import_allowed"],
            component["source_import_allowed"],
        )

    def test_unapproved_source_composition_cannot_claim_adoption_pass(self):
        pin = self.load(PIN_PATH)
        evidence = self.load(EVIDENCE_PATH)
        self.assertEqual(
            pin["source_import_allowed"],
            "PENDING_EXACT_COMPOSITION_AND_NOTICE_REVIEW",
        )
        self.assertEqual(evidence["status"], "INCONCLUSIVE")
        self.assertFalse(evidence["live_trading_authority_granted"])
        self.assertTrue(evidence["blockers"])
        self.assertEqual(
            set(evidence["probes"]),
            {
                "source_composition",
                "embedding",
                "windows_build",
                "linux_build",
                "packaging",
                "decimal_behavior",
                "event_ordering",
                "restart_reconciliation",
                "adapter_isolation",
            },
        )
        self.assertTrue(
            all(
                status == "INCONCLUSIVE"
                for status in evidence["probes"].values()
            )
        )

    def test_adoption_evidence_is_bound_to_selected_lean_identity(self):
        pin = self.load(PIN_PATH)
        evidence = self.load(EVIDENCE_PATH)
        self.assertEqual(evidence["lean_commit"], pin["commit"])
        self.assertEqual(evidence["lean_tree"], pin["tree"])
        self.assertRegex(evidence["foundation_base_sha"], r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
