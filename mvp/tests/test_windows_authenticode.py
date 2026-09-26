from hashlib import sha256
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

import tools.windows_authenticode as auth
from tools.windows_authenticode import (
    AuthenticodePolicy,
    AuthenticodeSigningError,
    azure_metadata_bytes,
    load_canonical_authenticode_policy,
    verify_velopack_authenticode,
)


SIGNER = "A" * 40
TIMESTAMP = "B" * 40


class WindowsAuthenticodeTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def policy(self):
        return AuthenticodePolicy(
            enabled=True,
            backend="AZURE_ARTIFACT_SIGNING",
            endpoint="https://eus.codesigning.azure.net/",
            code_signing_account_name="autotrade-signing",
            certificate_profile_name="production",
            timestamp_required=True,
        )

    def write_policy(self, value):
        path = self.root / "authenticode-policy.json"
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def test_committed_policy_is_disabled_and_cannot_select_signer(self):
        policy, digest = load_canonical_authenticode_policy()
        self.assertFalse(policy.enabled)
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        with self.assertRaisesRegex(AuthenticodeSigningError, "disabled"):
            azure_metadata_bytes(policy)

    def test_enabled_policy_generates_only_canonical_azure_metadata(self):
        policy = self.policy()
        payload = json.loads(azure_metadata_bytes(policy))
        self.assertEqual(
            payload,
            {
                "Endpoint": "https://eus.codesigning.azure.net/",
                "CodeSigningAccountName": "autotrade-signing",
                "CertificateProfileName": "production",
            },
        )

    def test_policy_rejects_caller_redirect_and_untimestamped_modes(self):
        for kwargs, message in (
            (
                {"endpoint": "http://eus.codesigning.azure.net/"},
                "canonical Azure signing HTTPS endpoint",
            ),
            (
                {"endpoint": "https://evil.example/"},
                "canonical Azure signing HTTPS endpoint",
            ),
            (
                {"endpoint": "https://eus.codesigning.azure.net/?profile=evil"},
                "canonical Azure signing HTTPS endpoint",
            ),
            (
                {"timestamp_required": False},
                "must require a timestamp",
            ),
        ):
            with self.subTest(kwargs=kwargs):
                values = {
                    "enabled": True,
                    "backend": "AZURE_ARTIFACT_SIGNING",
                    "endpoint": "https://eus.codesigning.azure.net/",
                    "code_signing_account_name": "autotrade-signing",
                    "certificate_profile_name": "production",
                    "timestamp_required": True,
                }
                values.update(kwargs)
                with self.assertRaisesRegex(AuthenticodeSigningError, message):
                    AuthenticodePolicy(**values)

    def test_disabled_policy_cannot_smuggle_signer_identity(self):
        with self.assertRaisesRegex(
            AuthenticodeSigningError,
            "disabled Authenticode policy cannot select a signer profile",
        ):
            AuthenticodePolicy(
                enabled=False,
                backend="AZURE_ARTIFACT_SIGNING",
                endpoint="https://eus.codesigning.azure.net/",
                code_signing_account_name="autotrade-signing",
                certificate_profile_name="production",
                timestamp_required=True,
            )

    def test_policy_loader_rejects_duplicate_fields_and_extra_authority(self):
        duplicate = self.root / "duplicate.json"
        duplicate.write_text(
            '{"schema_version":"1.0.0","schema_version":"2.0.0",'
            '"enabled":false,"backend":"AZURE_ARTIFACT_SIGNING",'
            '"endpoint":null,"code_signing_account_name":null,'
            '"certificate_profile_name":null,"timestamp_required":true}\n',
            encoding="utf-8",
        )
        with (
            patch.object(auth, "AUTHENTICODE_POLICY_PATH", duplicate),
            self.assertRaisesRegex(AuthenticodeSigningError, "duplicate JSON field"),
        ):
            load_canonical_authenticode_policy()

        extra = self.write_policy(
            {
                "schema_version": "1.0.0",
                "enabled": False,
                "backend": "AZURE_ARTIFACT_SIGNING",
                "endpoint": None,
                "code_signing_account_name": None,
                "certificate_profile_name": None,
                "timestamp_required": True,
                "sign_params": "/f candidate.pfx",
            }
        )
        with (
            patch.object(auth, "AUTHENTICODE_POLICY_PATH", extra),
            self.assertRaisesRegex(AuthenticodeSigningError, "fields do not match"),
        ):
            load_canonical_authenticode_policy()

    def make_signed_outputs(self, *, version="1.2.3", duplicate_main=False):
        setup = self.root / "AutoTrade-Setup.exe"
        setup.write_bytes(b"signed setup bytes")
        package = self.root / f"AutoTrade-{version}-full.nupkg"
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("lib/app/AutoTrade.Desktop.exe", b"signed desktop")
            archive.writestr("lib/app/Update.exe", b"signed update")
            if duplicate_main:
                archive.writestr("other/AutoTrade.Desktop.exe", b"second desktop")
        feed = self.root / "releases.win.json"
        feed.write_text('{"channel":"win"}\n', encoding="utf-8")
        result = []
        for path in (setup, package, feed):
            data = path.read_bytes()
            result.append(
                (path, "sha256:" + sha256(data).hexdigest(), len(data))
            )
        return result

    @staticmethod
    def valid_signature(path):
        return {
            "signer_thumbprint": SIGNER,
            "signer_subject": "CN=AutoTrade Test Signer",
            "timestamp_thumbprint": TIMESTAMP,
        }

    def test_release_verifier_binds_setup_main_and_update_to_one_signer(self):
        generated = self.make_signed_outputs()
        with (
            patch.object(auth, "assert_windows_signing_environment"),
            patch.object(
                auth,
                "_verify_authenticode_file",
                side_effect=self.valid_signature,
            ) as verifier,
        ):
            evidence = verify_velopack_authenticode(
                generated,
                version="1.2.3",
                policy=self.policy(),
                policy_sha256="sha256:" + "c" * 64,
            )
        self.assertEqual(evidence["signer_thumbprint"], SIGNER)
        self.assertEqual(evidence["backend"], "AZURE_ARTIFACT_SIGNING")
        self.assertEqual(len(evidence["verified_files"]), 3)
        paths = {item["path"] for item in evidence["verified_files"]}
        self.assertIn("AutoTrade-Setup.exe", paths)
        self.assertTrue(any(path.endswith("!/lib/app/AutoTrade.Desktop.exe") for path in paths))
        self.assertTrue(any(path.endswith("!/lib/app/Update.exe") for path in paths))
        self.assertEqual(verifier.call_count, 3)

    def test_release_verifier_rejects_duplicate_packaged_main_identity(self):
        generated = self.make_signed_outputs(duplicate_main=True)
        with (
            patch.object(auth, "assert_windows_signing_environment"),
            patch.object(auth, "_verify_authenticode_file", side_effect=self.valid_signature),
            self.assertRaisesRegex(AuthenticodeSigningError, "duplicate AutoTrade.Desktop.exe"),
        ):
            verify_velopack_authenticode(
                generated,
                version="1.2.3",
                policy=self.policy(),
                policy_sha256="sha256:" + "c" * 64,
            )

    def test_release_verifier_rejects_split_signer_identity(self):
        generated = self.make_signed_outputs()
        calls = 0

        def verifier(path):
            nonlocal calls
            calls += 1
            value = self.valid_signature(path)
            if calls == 3:
                value = dict(value)
                value["signer_thumbprint"] = "D" * 40
            return value

        with (
            patch.object(auth, "assert_windows_signing_environment"),
            patch.object(auth, "_verify_authenticode_file", side_effect=verifier),
            self.assertRaisesRegex(AuthenticodeSigningError, "one signer identity"),
        ):
            verify_velopack_authenticode(
                generated,
                version="1.2.3",
                policy=self.policy(),
                policy_sha256="sha256:" + "c" * 64,
            )

    def test_powershell_result_requires_valid_status_and_timestamp(self):
        path = self.root / "artifact.exe"
        path.write_bytes(b"artifact")

        def completed(payload):
            return subprocess.CompletedProcess(
                ["powershell"],
                0,
                stdout=json.dumps(payload),
                stderr="",
            )

        base = {
            "status": "Valid",
            "signer_thumbprint": SIGNER,
            "signer_subject": "CN=AutoTrade",
            "timestamp_thumbprint": TIMESTAMP,
        }
        with (
            patch.object(auth, "_trusted_windows_powershell", return_value=Path("powershell.exe")),
            patch.object(auth, "_powershell_environment", return_value={}),
        ):
            value = auth._verify_authenticode_file(
                path,
                runner=lambda *args, **kwargs: completed(base),
            )
            self.assertEqual(value["signer_thumbprint"], SIGNER)

            for mutation, message in (
                ({"status": "NotSigned"}, "not valid"),
                ({"timestamp_thumbprint": None}, "timestamp certificate is missing"),
                ({"signer_thumbprint": "not-a-thumbprint"}, "thumbprint is not canonical"),
            ):
                payload = dict(base)
                payload.update(mutation)
                with self.subTest(mutation=mutation):
                    with self.assertRaisesRegex(AuthenticodeSigningError, message):
                        auth._verify_authenticode_file(
                            path,
                            runner=lambda *args, payload=payload, **kwargs: completed(payload),
                        )


if __name__ == "__main__":
    unittest.main()
