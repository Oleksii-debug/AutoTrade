from dataclasses import replace
import unittest

from mvp.autotrade_mvp.provider_account_cut import (
    ProviderAccountCutError,
    ProviderAccountCutIdentity,
)
from mvp.autotrade_mvp.provider_domain import ProviderFinancialScope


D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
D3 = "sha256:" + "3" * 64
D4 = "sha256:" + "4" * 64
D5 = "sha256:" + "5" * 64
Q1 = "provider-qualification:sha256:" + "1" * 64
Q2 = "provider-qualification:sha256:" + "2" * 64


def scope(provider_environment: str = "TESTNET") -> ProviderFinancialScope:
    return ProviderFinancialScope(
        provider_id="BYBIT",
        runtime_environment="PAPER",
        provider_environment=provider_environment,
        entity_policy_id="LINEAR_ACCOUNT_V1",
    )


def account_cut(**overrides) -> ProviderAccountCutIdentity:
    values = {
        "provider_scope": scope(),
        "account_id": "account-1",
        "acquisition_mode": "SERIALIZED_ACQUISITION_GENERATION",
        "acquisition_id": "acquisition-1",
        "acquisition_generation": 7,
        "acquisition_journal_sequence_cut": 91,
        "qualification_identity_digest": Q1,
        "consistency_method_id": "snapshot-readback-v1",
        "consistency_method_version": 1,
        "origin_binding_set_digest": D2,
        "stream_binding_set_digest": D3,
        "backfill_binding_set_digest": D4,
        "coverage_window_digest": D5,
        "provider_native_generation_token": None,
    }
    values.update(overrides)
    return ProviderAccountCutIdentity(**values)


class ProviderAccountCutIdentityTests(unittest.TestCase):
    def test_identity_is_deterministic_and_versioned(self):
        value = account_cut()
        self.assertEqual(value.content_digest, account_cut().content_digest)
        self.assertEqual(value.payload()["schema_version"], "1.0.0")
        self.assertTrue(value.content_digest.startswith("provider-account-cut:sha256:"))

    def test_each_authority_dimension_changes_account_cut_identity(self):
        original = account_cut()
        changes = {
            "provider_scope": scope("DEMO"),
            "account_id": "account-2",
            "acquisition_id": "acquisition-2",
            "acquisition_generation": 8,
            "acquisition_journal_sequence_cut": 92,
            "qualification_identity_digest": Q2,
            "consistency_method_id": "stream-watermark-v1",
            "consistency_method_version": 2,
            "origin_binding_set_digest": D3,
            "stream_binding_set_digest": D4,
            "backfill_binding_set_digest": D5,
            "coverage_window_digest": D1,
        }
        for name, changed in changes.items():
            with self.subTest(field=name):
                self.assertNotEqual(
                    replace(original, **{name: changed}).content_digest,
                    original.content_digest,
                )

    def test_qualification_identity_uses_provider_qualification_namespace(self):
        value = account_cut()
        self.assertEqual(value.qualification_identity_digest, Q1)
        self.assertEqual(value.payload()["qualification_identity_digest"], Q1)

        for invalid in (
            D1,
            "1" * 64,
            "provider-qualification:sha256:" + "A" * 64,
            "provider-account-cut:sha256:" + "1" * 64,
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ProviderAccountCutError,
                    "provider-qualification:sha256",
                ):
                    account_cut(qualification_identity_digest=invalid)

    def test_acquisition_mode_must_already_be_canonical(self):
        for invalid in (
            "serialized_acquisition_generation",
            "provider_native_generation",
            "Serialized_Acquisition_Generation",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ProviderAccountCutError,
                    "exact canonical acquisition model",
                ):
                    account_cut(acquisition_mode=invalid)

    def test_provider_native_generation_requires_exact_token(self):
        with self.assertRaisesRegex(
            ProviderAccountCutError,
            "provider_native_generation_token is required",
        ):
            account_cut(
                acquisition_mode="PROVIDER_NATIVE_GENERATION",
                provider_native_generation_token=None,
            )

        native = account_cut(
            acquisition_mode="PROVIDER_NATIVE_GENERATION",
            provider_native_generation_token="provider-generation-123",
        )
        self.assertNotEqual(native.content_digest, account_cut().content_digest)

    def test_serialized_generation_rejects_provider_native_token(self):
        with self.assertRaisesRegex(
            ProviderAccountCutError,
            "serialized acquisition cannot carry",
        ):
            account_cut(provider_native_generation_token="provider-generation-123")

    def test_provider_scope_must_be_exact_shared_scope(self):
        class HostileScope(ProviderFinancialScope):
            pass

        hostile = HostileScope(
            provider_id="BYBIT",
            runtime_environment="PAPER",
            provider_environment="TESTNET",
            entity_policy_id="LINEAR_ACCOUNT_V1",
        )
        with self.assertRaisesRegex(ProviderAccountCutError, "exact ProviderFinancialScope"):
            account_cut(provider_scope=hostile)

    def test_boolean_generations_and_noncanonical_digests_fail_closed(self):
        with self.assertRaises(ProviderAccountCutError):
            account_cut(acquisition_generation=True)
        with self.assertRaises(ProviderAccountCutError):
            account_cut(consistency_method_version=True)
        with self.assertRaises(ProviderAccountCutError):
            account_cut(origin_binding_set_digest="2" * 64)


if __name__ == "__main__":
    unittest.main()
