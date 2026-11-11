from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.import_groq_credentials import (
    GroqCredentialImportError,
    extract_unique_groq_keys,
    import_groq_credentials,
)


def _key(letter: str) -> str:
    return "gsk_" + letter * 48


class ImportGroqCredentialsTests(unittest.TestCase):
    def test_imports_four_ordered_profiles_without_returning_secrets(self):
        with TemporaryDirectory() as root:
            source = Path(root) / "owner-keys.txt"
            keys = [_key(letter) for letter in "ABCD"]
            source.write_text("\nnotes\n" + "\n".join(keys), encoding="utf-8")
            calls = []

            def store(**kwargs):
                calls.append(kwargs)
                return f"AutoTrade/AI/v1/{kwargs['provider']}/{kwargs['profile']}"

            result = import_groq_credentials(source, store=store)

            self.assertEqual(result["profile_count"], 4)
            self.assertEqual(
                [item["profile"] for item in calls],
                [f"groq-account-{index}" for index in range(1, 5)],
            )
            self.assertEqual([item["key"] for item in calls], keys)
            self.assertNotIn("gsk_", repr(result))
            self.assertFalse(result["secret_material_emitted"])

    def test_duplicate_or_wrong_count_is_rejected_before_store(self):
        with TemporaryDirectory() as root:
            source = Path(root) / "owner-keys.txt"
            source.write_text("\n".join([_key("A"), _key("A"), _key("B")]))
            with self.assertRaisesRegex(GroqCredentialImportError, "exactly four"):
                extract_unique_groq_keys(source)

    def test_oversized_source_is_rejected(self):
        with TemporaryDirectory() as root:
            source = Path(root) / "owner-keys.txt"
            source.write_bytes(b"x" * (64 * 1024 + 1))
            with self.assertRaisesRegex(GroqCredentialImportError, "size"):
                extract_unique_groq_keys(source)


if __name__ == "__main__":
    unittest.main()
