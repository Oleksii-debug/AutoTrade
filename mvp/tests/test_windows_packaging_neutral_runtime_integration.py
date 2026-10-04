import ast
from pathlib import Path
import unittest

import autotrade_runtime.artifacts.durable_publish as neutral_publish
import tools.build_windows_bundle as windows_bundle
import tools.build_windows_install_manifest as install_manifest


ROOT = Path(__file__).resolve().parents[2]
PACKAGING_TOOLS = (
    "tools/build_windows_bundle.py",
    "tools/build_windows_install_manifest.py",
)


def _artifact_imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    return tuple(imported)


class WindowsPackagingNeutralRuntimeIntegrationTests(unittest.TestCase):
    def test_packaging_tools_bind_neutral_durable_publication(self):
        self.assertIs(
            windows_bundle.atomic_write_stream_with_sha256_sidecar,
            neutral_publish.atomic_write_stream_with_sha256_sidecar,
        )
        self.assertIs(
            windows_bundle.validate_publication_destination,
            neutral_publish.validate_publication_destination,
        )
        self.assertIs(
            install_manifest.atomic_write_bytes_with_sha256_sidecar,
            neutral_publish.atomic_write_bytes_with_sha256_sidecar,
        )
        self.assertIs(
            install_manifest.validate_publication_destination,
            neutral_publish.validate_publication_destination,
        )

    def test_packaging_tools_do_not_directly_import_research_artifact_authority(self):
        offenders = {}
        for relative in PACKAGING_TOOLS:
            imports = _artifact_imports(ROOT / relative)
            bad = tuple(
                name
                for name in imports
                if (
                    name == "research.autotrade_research.artifacts"
                    or name.startswith("research.autotrade_research.artifacts.")
                    or name == "autotrade_research.artifacts"
                    or name.startswith("autotrade_research.artifacts.")
                )
            )
            if bad:
                offenders[relative] = bad
        self.assertEqual(offenders, {})


if __name__ == "__main__":
    unittest.main()
