import ast
from pathlib import Path
import unittest

from autotrade_runtime.artifacts import _root_authority
from autotrade_runtime.resource_lock import ResourceLockError

from autotrade_research.artifacts import (
    ArtifactStore as PackageArtifactStore,
    CANONICAL_ARTIFACT_STORE_MODULE,
    trusted_authenticated_reader,
)
from autotrade_research.artifacts.store import ArtifactStore as CanonicalArtifactStore


ROOT = Path(__file__).resolve().parents[1] / "autotrade_research"


def _imports_legacy_content_store(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "autotrade_research.artifacts.content_store"
                or alias.name.endswith(".artifacts.content_store")
                for alias in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if (
                module == "autotrade_research.artifacts.content_store"
                or module.endswith(".artifacts.content_store")
                or (node.level == 1 and module == "content_store")
            ):
                return True
            if (
                node.level == 1
                and module == ""
                and any(alias.name == "content_store" for alias in node.names)
            ):
                return True
    return False


class ArtifactAuthorityTests(unittest.TestCase):
    def test_package_exports_the_rights_bound_canonical_store(self):
        self.assertIs(PackageArtifactStore, CanonicalArtifactStore)
        self.assertEqual(
            CANONICAL_ARTIFACT_STORE_MODULE,
            "autotrade_runtime.artifacts.store",
        )

    def test_trusted_root_rejects_text_subclass_before_virtual_dispatch(self):
        touched: list[str] = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile trusted-root normalization")

            def __fspath__(self):
                touched.append("fspath")
                raise AssertionError("hostile trusted-root path conversion")

        with self.assertRaisesRegex(TypeError, "exact string or Path"):
            trusted_authenticated_reader(HostileText("/not-authoritative"))
        self.assertEqual(touched, [])

    def test_trusted_root_rejects_path_subclass_before_fspath_dispatch(self):
        touched: list[str] = []

        class HostilePath(type(Path())):
            def __fspath__(self):
                touched.append("fspath")
                raise AssertionError("hostile trusted-root path conversion")

        with self.assertRaisesRegex(TypeError, "exact string or Path"):
            trusted_authenticated_reader(HostilePath("/not-authoritative"))
        self.assertEqual(touched, [])

    def test_configured_root_key_rejects_text_subclass_before_virtual_dispatch(self):
        touched: list[str] = []

        class HostileText(str):
            def encode(self, *args, **kwargs):
                touched.append("encode")
                raise AssertionError("hostile configured-root hashing")

        class StoreLike:
            pass

        store = StoreLike()
        store._configured_artifact_root_key = HostileText("/not-authoritative")
        with self.assertRaisesRegex(ResourceLockError, "path key is unavailable"):
            _root_authority._windows_path_mutex_name(store)
        self.assertEqual(touched, [])

    def test_production_code_cannot_reintroduce_legacy_content_store_authority(self):
        offenders = []
        for path in sorted(ROOT.rglob("*.py")):
            if path.name == "content_store.py":
                continue
            if _imports_legacy_content_store(path):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders,
            [],
            "legacy content_store must remain isolated from production modules",
        )

    def test_legacy_import_guard_ignores_plain_text_mentions(self):
        source = '"""content_store compatibility note."""\nVALUE = "artifacts.content_store"\n'
        tree = ast.parse(source)
        self.assertFalse(
            any(
                isinstance(node, (ast.Import, ast.ImportFrom))
                for node in ast.walk(tree)
            )
        )


if __name__ == "__main__":
    unittest.main()
