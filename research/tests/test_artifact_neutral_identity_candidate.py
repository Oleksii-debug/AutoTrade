from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class NeutralArtifactIdentityCandidateTests(unittest.TestCase):
    def _run_isolated(self, body: str, *, research_path: bool = True) -> None:
        prefix = [
            "import importlib, sys",
            f"sys.path.insert(0, {str(ROOT)!r})",
        ]
        if research_path:
            prefix.append(f"sys.path.insert(0, {str(ROOT / 'research')!r})")
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-c", ";".join(prefix) + "\n" + body],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_product_only_import_has_no_research_dependency(self):
        self._run_isolated(
            """
import autotrade_runtime.artifacts as product
assert product.CANONICAL_ARTIFACT_STORE_MODULE == "autotrade_runtime.artifacts.store"
assert product.ArtifactStore.__module__ == "autotrade_runtime.artifacts.store"
assert not any(name == "research" or name.startswith("research.") for name in sys.modules)
assert not any(name == "autotrade_research" or name.startswith("autotrade_research.") for name in sys.modules)
""",
            research_path=False,
        )

    def test_product_first_research_aliases_preserve_exact_identity(self):
        self._run_isolated(
            """
import autotrade_runtime.artifacts as product
import autotrade_research.artifacts as research_artifacts
assert research_artifacts.ArtifactStore is product.ArtifactStore
assert research_artifacts.trusted_authenticated_reader is product.trusted_authenticated_reader
assert importlib.import_module("autotrade_research.artifacts.store") is importlib.import_module("autotrade_runtime.artifacts.store")
assert importlib.import_module("autotrade_research.artifacts._root_authority") is importlib.import_module("autotrade_runtime.artifacts._root_authority")
assert importlib.import_module("autotrade_research.artifacts.resource_lock") is importlib.import_module("autotrade_runtime.resource_lock")
import autotrade_research.io.strict_json as research_json
import autotrade_runtime.strict_json as runtime_json
assert research_json.strict_json_loads is runtime_json.strict_json_loads
assert research_json.jsonl_bytes_are_blank is runtime_json.jsonl_bytes_are_blank
assert research_json.DuplicateJsonKeyError is runtime_json.DuplicateJsonKeyError
assert research_json.NonStandardJsonConstantError is runtime_json.NonStandardJsonConstantError
assert research_json.InvalidJsonDomainError is runtime_json.InvalidJsonDomainError
""",
        )

    def test_research_first_resolves_same_neutral_modules(self):
        self._run_isolated(
            """
import autotrade_research.artifacts as research_artifacts
import autotrade_runtime.artifacts as product
assert research_artifacts.ArtifactStore is product.ArtifactStore
assert importlib.import_module("autotrade_research.artifacts.store") is importlib.import_module("autotrade_runtime.artifacts.store")
assert importlib.import_module("autotrade_research.artifacts._root_authority_failure_fix") is importlib.import_module("autotrade_runtime.artifacts._root_authority_failure_fix")
""",
        )

    def test_repo_qualified_research_name_also_resolves_neutral_identity(self):
        self._run_isolated(
            """
import research.autotrade_research.artifacts as research_artifacts
import autotrade_runtime.artifacts as product
assert research_artifacts.ArtifactStore is product.ArtifactStore
assert importlib.import_module("research.autotrade_research.artifacts.store") is importlib.import_module("autotrade_runtime.artifacts.store")
assert importlib.import_module("research.autotrade_research.artifacts._root_authority") is importlib.import_module("autotrade_runtime.artifacts._root_authority")
repo_json = importlib.import_module("research.autotrade_research.io.strict_json")
runtime_json = importlib.import_module("autotrade_runtime.strict_json")
assert repo_json.strict_json_loads is runtime_json.strict_json_loads
assert repo_json.InvalidJsonDomainError is runtime_json.InvalidJsonDomainError
""",
            research_path=False,
        )


if __name__ == "__main__":
    unittest.main()
