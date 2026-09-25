from hashlib import sha1
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tools.build_provenance_manifest import (
    OUTPUT,
    ROOT,
    git_blob_sha,
    rendered_manifest,
)


class ReleaseManifestPortabilityTests(unittest.TestCase):
    def test_repository_text_blob_sha_is_identical_for_lf_and_crlf_checkout(self):
        canonical = b"alpha\nbeta\n"
        expected = sha1(
            b"blob " + str(len(canonical)).encode("ascii") + b"\0" + canonical
        ).hexdigest()

        provenance_root = ROOT / "provenance"
        with TemporaryDirectory(dir=provenance_root) as directory:
            root = Path(directory)
            lf = root / "lf.txt"
            crlf = root / "crlf.txt"
            lf.write_bytes(canonical)
            crlf.write_bytes(canonical.replace(b"\n", b"\r\n"))

            self.assertEqual(git_blob_sha(lf), expected)
            self.assertEqual(git_blob_sha(crlf), expected)
            self.assertEqual(git_blob_sha(lf), git_blob_sha(crlf))

    def test_real_content_change_changes_blob_identity(self):
        with TemporaryDirectory(dir=ROOT / "provenance") as directory:
            path = Path(directory) / "content.txt"
            path.write_bytes(b"alpha\nbeta\n")
            original = git_blob_sha(path)
            path.write_bytes(b"alpha\ngamma\n")
            self.assertNotEqual(git_blob_sha(path), original)

    def test_unavailable_or_outside_repository_path_fails_closed(self):
        missing = ROOT / "provenance" / ".missing-provenance-input"
        with self.assertRaisesRegex(ValueError, "unavailable"):
            git_blob_sha(missing)

        with TemporaryDirectory() as directory:
            outside = Path(directory) / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "inside repository"):
                git_blob_sha(outside)

    def test_manifest_check_is_read_only_and_byte_stable(self):
        before = OUTPUT.read_bytes()
        result = subprocess.run(
            [sys.executable, "tools/build_provenance_manifest.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(OUTPUT.read_bytes(), before)
        self.assertEqual(before, rendered_manifest().encode("utf-8"))
        self.assertNotIn(b"\r\n", before)


if __name__ == "__main__":
    unittest.main()
