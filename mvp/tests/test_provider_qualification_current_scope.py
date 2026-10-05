from __future__ import annotations

from dataclasses import replace
import unittest

from mvp.autotrade_mvp.provider_domain import ProviderFinancialScope
from mvp.autotrade_mvp.provider_qualification_current_scope import (
    ProviderQualificationCurrentScope,
    ProviderQualificationCurrentScopeError,
)


PACKAGE_DIGEST = "sha256:" + "1" * 64
OTHER_PACKAGE_DIGEST = "sha256:" + "2" * 64


def provider_scope(*, provider_environment: str = "TESTNET") -> ProviderFinancialScope:
    return ProviderFinancialScope(
        provider_id="BYBIT",
        runtime_environment="PAPER",
        provider_environment=provider_environment,
        entity_policy_id="LINEAR_ORDER_V1",
    )


def current_scope(**overrides) -> ProviderQualificationCurrentScope:
    values = {
        "provider_scope": provider_scope(),
        "product_family": "LINEAR_PERPETUAL",
        "adapter_source_git_sha": "a" * 40,
        "packaged_artifact_digest": PACKAGE_DIGEST,
        "protocol_id": "provider-route-v1",
        "protocol_version": "1.0.0",
    }
    values.update(overrides)
    return ProviderQualificationCurrentScope(**values)


class ProviderQualificationCurrentScopeTests(unittest.TestCase):
    def test_current_scope_is_deterministic_and_versioned(self):
        scope = current_scope()
        self.assertEqual(scope, current_scope())
        self.assertEqual(scope.content_digest, current_scope().content_digest)
        self.assertEqual(scope.payload()["schema_version"], "1.0.0")
        self.assertTrue(
            scope.content_digest.startswith("provider-qualification-current-scope:sha256:")
        )

    def test_every_currentness_dimension_changes_scope_identity(self):
        original = current_scope()
        changes = {
            "provider_scope": provider_scope(provider_environment="DEMO"),
            "product_family": "SPOT",
            "adapter_source_git_sha": "b" * 40,
            "packaged_artifact_digest": OTHER_PACKAGE_DIGEST,
            "protocol_id": "provider-route-v2",
            "protocol_version": "2.0.0",
        }
        for name, changed in changes.items():
            with self.subTest(field=name):
                self.assertNotEqual(
                    replace(original, **{name: changed}).content_digest,
                    original.content_digest,
                )

    def test_scope_deliberately_has_no_campaign_identity_escape_hatch(self):
        payload = current_scope().payload()
        self.assertNotIn("campaign_id", payload)
        self.assertNotIn("campaign_version", payload)
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "provider_scope",
                "product_family",
                "adapter_source_git_sha",
                "packaged_artifact_digest",
                "protocol_id",
                "protocol_version",
            },
        )

    def test_product_family_is_canonicalized_but_authority_tokens_stay_exact(self):
        scope = current_scope(product_family="linear_perpetual")
        self.assertEqual(scope.product_family, "LINEAR_PERPETUAL")
        self.assertEqual(scope.protocol_id, "provider-route-v1")
        self.assertEqual(scope.protocol_version, "1.0.0")

    def test_noncanonical_inputs_fail_closed(self):
        for kwargs in (
            {"adapter_source_git_sha": "A" * 40},
            {"adapter_source_git_sha": "a" * 39},
            {"packaged_artifact_digest": "1" * 64},
            {"protocol_id": " provider-route-v1"},
            {"protocol_version": ""},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ProviderQualificationCurrentScopeError):
                    current_scope(**kwargs)

    def test_provider_scope_subclass_is_not_currentness_authority(self):
        class HostileScope(ProviderFinancialScope):
            pass

        hostile = HostileScope(
            provider_id="BYBIT",
            runtime_environment="PAPER",
            provider_environment="TESTNET",
            entity_policy_id="LINEAR_ORDER_V1",
        )
        with self.assertRaisesRegex(
            ProviderQualificationCurrentScopeError,
            "exact ProviderFinancialScope",
        ):
            current_scope(provider_scope=hostile)


if __name__ == "__main__":
    unittest.main()
