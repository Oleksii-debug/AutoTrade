from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tools.build_provenance_manifest import (
    QUALIFICATION_TRUST_POLICY_COMPONENT_ID,
    QUALIFICATION_TRUST_POLICY_COMPONENT_KIND,
    QUALIFICATION_TRUST_POLICY_COMPONENT_PATH,
    QUALIFICATION_TRUST_POLICY_COMPONENT_VERSION,
    _trusted_git_environment,
    _trusted_git_executable,
    qualification_trust_policy_composition,
    qualification_trust_policy_digest_from_git_source,
    qualification_trust_policy_digest_from_source,
)


DIGEST = "sha256:" + "1" * 64


def policy_component(*, digest: str = DIGEST, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "component_id": QUALIFICATION_TRUST_POLICY_COMPONENT_ID,
        "kind": QUALIFICATION_TRUST_POLICY_COMPONENT_KIND,
        "path": QUALIFICATION_TRUST_POLICY_COMPONENT_PATH,
        "version": QUALIFICATION_TRUST_POLICY_COMPONENT_VERSION,
        "sha256": digest,
    }
    value.update(overrides)
    return value


class QualificationTrustPolicyPinSourceTests(unittest.TestCase):
    def write_source(self, body: str) -> Path:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "qualification_attestation.py"
        path.write_text(body, encoding="utf-8")
        return path

    def test_none_pin_remains_explicitly_unavailable(self):
        path = self.write_source(
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256: str | None = None\n"
        )
        self.assertIsNone(qualification_trust_policy_digest_from_source(path))

    def test_literal_canonical_pin_is_read_without_importing_module(self):
        path = self.write_source(
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256: str | None = "
            + repr(DIGEST)
            + "\nraise RuntimeError('must never execute')\n"
        )
        self.assertEqual(
            qualification_trust_policy_digest_from_source(path),
            DIGEST,
        )

    def test_dynamic_pin_expression_fails_closed(self):
        path = self.write_source(
            "VALUE = " + repr(DIGEST) + "\n"
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = VALUE\n"
        )
        with self.assertRaisesRegex(ValueError, "literal source definition"):
            qualification_trust_policy_digest_from_source(path)

    def test_tuple_or_augmented_rebinding_fails_closed(self):
        for source in (
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = None\n"
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256, other = "
            + repr((DIGEST, "x"))
            + "\n",
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = None\n"
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 += 'x'\n",
        ):
            with self.subTest(source=source):
                path = self.write_source(source)
                with self.assertRaisesRegex(ValueError, "literal source definition"):
                    qualification_trust_policy_digest_from_source(path)

    def test_non_assignment_module_rebindings_fail_closed(self):
        cases = (
            (
                "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = "
                + repr(DIGEST)
                + "\ndef _CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256():\n"
                "    return None\n"
            ),
            (
                "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = "
                + repr(DIGEST)
                + "\nclass _CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256:\n"
                "    pass\n"
            ),
            (
                "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = "
                + repr(DIGEST)
                + "\nimport os as _CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256\n"
            ),
            (
                "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = "
                + repr(DIGEST)
                + "\nfrom os import path as _CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256\n"
            ),
            (
                "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = "
                + repr(DIGEST)
                + "\ndel _CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256\n"
            ),
            (
                "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = "
                + repr(DIGEST)
                + "\ntry:\n"
                "    raise RuntimeError()\n"
                "except RuntimeError as _CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256:\n"
                "    pass\n"
            ),
        )
        for source in cases:
            with self.subTest(source=source):
                path = self.write_source(source)
                with self.assertRaisesRegex(
                    ValueError,
                    "one literal source definition",
                ):
                    qualification_trust_policy_digest_from_source(path)

    def test_duplicate_pin_definition_fails_closed(self):
        path = self.write_source(
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = None\n"
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = "
            + repr(DIGEST)
            + "\n"
        )
        with self.assertRaisesRegex(ValueError, "one literal source definition"):
            qualification_trust_policy_digest_from_source(path)

    def test_noncanonical_literal_pin_fails_closed(self):
        path = self.write_source(
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = 'not-a-digest'\n"
        )
        with self.assertRaisesRegex(ValueError, "canonical SHA-256"):
            qualification_trust_policy_digest_from_source(path)

    def test_malformed_pin_source_fails_closed(self):
        path = self.write_source(
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = (\n"
        )
        with self.assertRaisesRegex(ValueError, "unavailable or invalid"):
            qualification_trust_policy_digest_from_source(path)

    def test_release_pin_is_read_from_selected_git_object_not_dirty_worktree(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source = root / "mvp" / "autotrade_mvp" / "qualification_attestation.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = None\n",
            encoding="utf-8",
        )

        trusted_git = _trusted_git_executable(source_root=root)

        def git(*args: str) -> str:
            completed = subprocess.run(
                [trusted_git, *args],
                cwd=root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=_trusted_git_environment(),
            )
            return completed.stdout.strip()

        git("init")
        git("config", "user.name", "AutoTrade Test")
        git("config", "user.email", "autotrade-test@example.invalid")
        git("add", "mvp/autotrade_mvp/qualification_attestation.py")
        git("commit", "-m", "pin source")
        source_sha = git("rev-parse", "HEAD")
        expected_blob = git(
            "rev-parse",
            f"{source_sha}:mvp/autotrade_mvp/qualification_attestation.py",
        )

        # The working tree now advertises a different pin. Release provenance
        # must remain tied to the selected source commit.
        source.write_text(
            "_CANONICAL_PACKAGED_QUALIFICATION_TRUST_POLICY_SHA256 = "
            + repr(DIGEST)
            + "\n",
            encoding="utf-8",
        )
        digest, blob_sha = qualification_trust_policy_digest_from_git_source(
            source_root=root,
            source_sha=source_sha,
        )
        self.assertIsNone(digest)
        self.assertEqual(blob_sha, expected_blob)


class QualificationTrustPolicyCompositionTests(unittest.TestCase):
    def test_exact_policy_component_is_accepted(self):
        document = {
            "components": [
                {
                    "component_id": "unrelated",
                    "kind": "runtime",
                    "path": "bin/runtime.exe",
                    "version": "1",
                    "sha256": "sha256:" + "2" * 64,
                },
                policy_component(),
            ]
        }
        self.assertEqual(
            qualification_trust_policy_composition(
                document,
                expected_digest=DIGEST,
            ),
            (True, None, DIGEST),
        )

    def test_missing_source_controlled_pin_fails_closed(self):
        self.assertEqual(
            qualification_trust_policy_composition(
                {"components": [policy_component()]},
                expected_digest=None,
            ),
            (False, "policy_pin_missing", None),
        )

    def test_missing_policy_component_fails_closed(self):
        self.assertEqual(
            qualification_trust_policy_composition(
                {"components": []},
                expected_digest=DIGEST,
            ),
            (False, "component_missing", None),
        )

    def test_non_array_components_fail_closed(self):
        self.assertEqual(
            qualification_trust_policy_composition(
                {"components": {}},
                expected_digest=DIGEST,
            ),
            (False, "components_missing", None),
        )

    def test_non_object_component_fails_closed(self):
        self.assertEqual(
            qualification_trust_policy_composition(
                {"components": ["not-an-object"]},
                expected_digest=DIGEST,
            ),
            (False, "component_not_object", None),
        )

    def test_policy_path_and_id_cannot_select_two_different_components(self):
        document = {
            "components": [
                policy_component(component_id="other"),
                policy_component(path="other/policy.json"),
            ]
        }
        self.assertEqual(
            qualification_trust_policy_composition(
                document,
                expected_digest=DIGEST,
            ),
            (False, "component_ambiguous", None),
        )

    def test_component_id_is_canonical(self):
        ok, reason, digest = qualification_trust_policy_composition(
            {"components": [policy_component(component_id="other")]},
            expected_digest=DIGEST,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "component_id_mismatch")
        self.assertIsNone(digest)

    def test_component_path_is_canonical(self):
        ok, reason, digest = qualification_trust_policy_composition(
            {"components": [policy_component(path="other/policy.json")]},
            expected_digest=DIGEST,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "path_mismatch")
        self.assertIsNone(digest)

    def test_component_kind_and_version_are_canonical(self):
        for field, replacement, expected_reason in (
            ("kind", "runtime", "kind_mismatch"),
            ("version", "caller-selected", "version_mismatch"),
        ):
            with self.subTest(field=field):
                ok, reason, digest = qualification_trust_policy_composition(
                    {"components": [policy_component(**{field: replacement})]},
                    expected_digest=DIGEST,
                )
                self.assertFalse(ok)
                self.assertEqual(reason, expected_reason)
                self.assertIsNone(digest)

    def test_policy_component_rejects_extra_caller_fields(self):
        ok, reason, digest = qualification_trust_policy_composition(
            {"components": [policy_component(extra_authority="caller")]},
            expected_digest=DIGEST,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "component_fields_mismatch")
        self.assertIsNone(digest)

    def test_composition_cannot_override_policy_digest(self):
        ok, reason, digest = qualification_trust_policy_composition(
            {"components": [policy_component(digest="sha256:" + "3" * 64)]},
            expected_digest=DIGEST,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "sha256_mismatch")
        self.assertIsNone(digest)

    def test_expected_digest_must_be_canonical(self):
        with self.assertRaisesRegex(ValueError, "canonical SHA-256"):
            qualification_trust_policy_composition(
                {"components": [policy_component()]},
                expected_digest="1" * 64,
            )


if __name__ == "__main__":
    unittest.main()
