from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]

CONSUMERS = (
    "mvp.autotrade_mvp.model_pricing_evidence",
    "mvp.autotrade_mvp.model_billing_evidence",
    "mvp.autotrade_mvp.model_observation_evidence",
    "mvp.autotrade_mvp.model_production_composition",
    "mvp.autotrade_mvp.durable_reservations",
    "mvp.autotrade_mvp.authority",
    "mvp.autotrade_mvp.qualification_attestation",
    "mvp.autotrade_mvp.reconciliation_journal",
    "mvp.autotrade_mvp.securities_borrow",
    "mvp.autotrade_mvp.bounded_real",
    "mvp.autotrade_mvp.recovery_qualification",
    "mvp.autotrade_mvp.release_candidate",
    "mvp.autotrade_mvp.supply_chain_qualification",
    "mvp.autotrade_mvp.asset_provider_crosswalk",
    "mvp.autotrade_mvp.windows_update",
    "mvp.autotrade_mvp.science_qualification",
)


class NeutralArtifactConsumerImportCandidateTests(unittest.TestCase):
    def test_terminal_consumers_import_without_research_tree_authority(self):
        module_list = repr(CONSUMERS)
        script = f'''
import importlib
import sys
sys.path.insert(0, {str(ROOT)!r})
for name in {module_list}:
    importlib.import_module(name)
assert not any(name == 'research' or name.startswith('research.') for name in sys.modules)
assert not any(name == 'autotrade_research' or name.startswith('autotrade_research.') for name in sys.modules)
import autotrade_runtime.artifacts as artifacts
assert artifacts.ArtifactStore.__module__ == 'autotrade_runtime.artifacts.store'
assert artifacts.CANONICAL_ARTIFACT_STORE_MODULE == 'autotrade_runtime.artifacts.store'
'''
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
