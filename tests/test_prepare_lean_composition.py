from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.qualification.prepare_lean_composition import (
    DOTNETZIP_NEW,
    DOTNETZIP_OLD,
    DRAWING_ANCHOR,
    DRAWING_REF,
    FILES,
    git_blob_digest,
    prepare,
)


class LeanCompositionTests(unittest.TestCase):
    def _tree(self, root: Path) -> Path:
        lean = root / "lean"
        (lean / "Compression").mkdir(parents=True)
        (lean / "Common").mkdir(parents=True)
        (lean / "Compression" / "QuantConnect.Compression.csproj").write_text(
            f"<Project>\n  {DOTNETZIP_OLD}\n</Project>\n",
            encoding="utf-8",
        )
        (lean / "Common" / "QuantConnect.csproj").write_text(
            f"<Project>\n  {DRAWING_ANCHOR}\n</Project>\n",
            encoding="utf-8",
        )
        return lean

    def _expected_blobs(self, lean: Path) -> dict[str, str]:
        return {
            relative: git_blob_digest((lean / relative).read_bytes())
            for relative in FILES
        }

    def test_prepare_replaces_only_approved_dependencies_and_is_hashed(self):
        with TemporaryDirectory() as directory:
            lean = self._tree(Path(directory))
            manifest = prepare(lean, expected_blobs=self._expected_blobs(lean))

            compression = (lean / "Compression" / "QuantConnect.Compression.csproj").read_text()
            common = (lean / "Common" / "QuantConnect.csproj").read_text()
            self.assertNotIn(DOTNETZIP_OLD, compression)
            self.assertIn(DOTNETZIP_NEW, compression)
            self.assertIn(DRAWING_REF, common)
            self.assertRegex(manifest["composition_sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(len(manifest["changes"]), 2)

    def test_prepare_fails_closed_if_upstream_anchor_changes(self):
        with TemporaryDirectory() as directory:
            lean = self._tree(Path(directory))
            path = lean / "Compression" / "QuantConnect.Compression.csproj"
            path.write_text("<Project />\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly one approved composition anchor"):
                prepare(lean, expected_blobs=self._expected_blobs(lean))

    def test_prepare_is_not_silently_reapplied(self):
        with TemporaryDirectory() as directory:
            lean = self._tree(Path(directory))
            expected = self._expected_blobs(lean)
            prepare(lean, expected_blobs=expected)
            with self.assertRaises(ValueError):
                prepare(lean, expected_blobs=expected)


    def test_prepare_rejects_anchor_preserving_upstream_drift(self):
        with TemporaryDirectory() as directory:
            lean = self._tree(Path(directory))
            expected = self._expected_blobs(lean)
            path = lean / "Compression" / "QuantConnect.Compression.csproj"
            path.write_text(
                path.read_text(encoding="utf-8").replace("<Project>", "<Project>\n  <!-- drift -->"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "upstream blob mismatch"):
                prepare(lean, expected_blobs=expected)

    def test_default_blob_map_rejects_noncanonical_fixture_tree(self):
        with TemporaryDirectory() as directory:
            lean = self._tree(Path(directory))
            with self.assertRaisesRegex(ValueError, "upstream blob mismatch"):
                prepare(lean)


if __name__ == "__main__":
    unittest.main()
