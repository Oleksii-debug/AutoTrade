from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)


class ArtifactCapabilityIngressTests(unittest.TestCase):
    def _capability_attributes(self) -> tuple[tuple[str, str], ...]:
        if sys.platform == "win32":
            return (
                ("root", "_namespace_root_handle"),
                ("manifests", "_retained_manifests_handle"),
                ("objects", "_retained_objects_handle"),
                ("staging", "_retained_staging_handle"),
            )
        return (
            ("root", "_namespace_root_fd"),
            ("manifests", "_retained_manifests_fd"),
            ("objects", "_retained_objects_fd"),
            ("staging", "_retained_staging_fd"),
        )

    def test_publication_store_rejects_polymorphic_capabilities_before_dispatch(self):
        touched: list[str] = []

        class HostileInt(int):
            def __int__(self):
                touched.append("int")
                raise AssertionError("hostile retained capability conversion")

            def __index__(self):
                touched.append("index")
                raise AssertionError("hostile retained capability indexing")

            def __bool__(self):
                touched.append("bool")
                raise AssertionError("hostile retained capability truth test")

            def __lt__(self, other):
                touched.append("lt")
                raise AssertionError("hostile retained capability comparison")

            def __eq__(self, other):
                touched.append("eq")
                raise AssertionError("hostile retained capability comparison")

        for name, attribute in self._capability_attributes():
            with self.subTest(capability=name), TemporaryDirectory() as directory:
                root = Path(directory) / "artifacts"
                publication_store = ArtifactStore(root)
                original = object.__getattribute__(publication_store, attribute)
                object.__setattr__(publication_store, attribute, HostileInt(original))
                try:
                    with self.assertRaisesRegex(
                        ArtifactIntegrityError,
                        f"retained artifact {name} capability is unavailable",
                    ):
                        trusted_authenticated_reader(
                            root,
                            publication_store=publication_store,
                        )
                finally:
                    object.__setattr__(publication_store, attribute, original)
        self.assertEqual(touched, [])

    def test_publication_store_rejects_negative_exact_capability(self):
        name, attribute = self._capability_attributes()[0]
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            publication_store = ArtifactStore(root)
            original = object.__getattribute__(publication_store, attribute)
            object.__setattr__(publication_store, attribute, -1)
            try:
                with self.assertRaisesRegex(
                    ArtifactIntegrityError,
                    f"retained artifact {name} capability is unavailable",
                ):
                    trusted_authenticated_reader(
                        root,
                        publication_store=publication_store,
                    )
            finally:
                object.__setattr__(publication_store, attribute, original)


if __name__ == "__main__":
    unittest.main()
