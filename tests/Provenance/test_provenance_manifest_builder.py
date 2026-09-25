from hashlib import sha1
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.build_provenance_manifest import git_blob_sha


class ProvenanceManifestBlobIdentityTests(unittest.TestCase):
    def test_git_blob_sha_is_stable_across_lf_and_crlf_checkouts(self):
        canonical = "alpha\nbeta\n".encode("utf-8")
        expected = sha1(
            b"blob " + str(len(canonical)).encode("ascii") + b"\0" + canonical
        ).hexdigest()

        with TemporaryDirectory() as directory:
            root = Path(directory)
            lf = root / "lf.txt"
            crlf = root / "crlf.txt"
            lf.write_bytes(canonical)
            crlf.write_bytes(b"alpha\r\nbeta\r\n")

            self.assertEqual(git_blob_sha(lf), expected)
            self.assertEqual(git_blob_sha(crlf), expected)
            self.assertEqual(git_blob_sha(lf), git_blob_sha(crlf))


if __name__ == "__main__":
    unittest.main()
