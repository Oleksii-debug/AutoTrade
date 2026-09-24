from pathlib import Path
import unittest

from autotrade_research.artifacts import (
    ArtifactStore as PackageArtifactStore,
    CANONICAL_ARTIFACT_STORE_MODULE,
)
from autotrade_research.artifacts.store import ArtifactStore as CanonicalArtifactStore


ROOT = Path(__file__).resolve().parents[1] / "autotrade_research"


class ArtifactAuthorityTests(unittest.TestCase):
    def test_package_exports_the_rights_bound_canonical_store(self):
        self.assertIs(PackageArtifactStore, CanonicalArtifactStore)
        self.assertEqual(
            CANONICAL_ARTIFACT_STORE_MODULE,
            "autotrade_research.artifacts.store",
        )

    def test_production_code_cannot_reintroduce_legacy_content_store_authority(self):
        offenders = []
        for path in sorted(ROOT.rglob("*.py")):
            if path.name == "content_store.py":
                continue
            text = path.read_text(encoding="utf-8")
            if (
                "artifacts.content_store" in text
                or "from .content_store import" in text
                or "from autotrade_research.artifacts.content_store import" in text
            ):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders,
            [],
            "legacy content_store must remain isolated from production modules",
        )


if __name__ == "__main__":
    unittest.main()
