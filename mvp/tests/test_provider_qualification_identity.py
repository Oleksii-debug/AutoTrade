from dataclasses import replace
import unittest

from mvp.autotrade_mvp.provider_domain import ProviderFinancialScope
from mvp.autotrade_mvp.provider_qualification_identity import (
    ProviderQualificationIdentity,
    ProviderQualificationIdentityError,
)


DIGESTS = ["sha256:" + char * 64 for char in "123456789a"]


def scope(provider_environment: str = "TESTNET") -> ProviderFinancialScope:
    return ProviderFinancialScope(
        provider_id="BYBIT",
        runtime_environment="PAPER",
        provider_environment=provider_environment,
        entity_policy_id="LINEAR_ORDER_V1",
    )


def qualification(**overrides) -> ProviderQualificationIdentity:
    values = {
        "provider_scope": scope(),
        "product_family": "LINEAR_PERPETUAL",
        "adapter_source_git_sha": "a" * 40,
        "packaged_artifact_digest": DIGESTS[0],
        "campaign_id": "bybit-linear-order-v1",
        "campaign_version": 1,
        "required_case_policy_digest": DIGESTS[1],
        "result_set_digest": DIGESTS[2],
        "route_semantics_digest": DIGESTS[3],
        "documentation_revision_digest": DIGESTS[4],
        "evidence_set_digest": DIGESTS[5],
        "attestation_digest": DIGESTS[6],
        "trust_policy_digest": DIGESTS[7],
        "issuer_identity_digest": DIGESTS[8],
        "verifier_identity_digest": DIGESTS[9],
    }
    values.update(overrides)
    return ProviderQualificationIdentity(**values)


class ProviderQualificationIdentityTests(unittest.TestCase):
    def test_identity_is_versioned_and_deterministic(self):
        value = qualification()
        self.assertEqual(value.content_digest, qualification().content_digest)
        self.assertEqual(value.payload()["schema_version"], "1.0.0")
        self.assertTrue(value.content_digest.startswith("provider-qualification:sha256:"))

    def test_every_qualification_authority_dimension_changes_identity(self):
        original = qualification()
        changes = {
            "provider_scope": scope("DEMO"),
            "product_family": "INVERSE_PERPETUAL",
            "adapter_source_git_sha": "b" * 40,
            "packaged_artifact_digest": DIGESTS[1],
            "campaign_id": "bybit-linear-order-v2",
            "campaign_version": 2,
            "required_case_policy_digest": DIGESTS[2],
            "result_set_digest": DIGESTS[3],
            "route_semantics_digest": DIGESTS[4],
            "documentation_revision_digest": DIGESTS[5],
            "evidence_set_digest": DIGESTS[6],
            "attestation_digest": DIGESTS[7],
            "trust_policy_digest": DIGESTS[8],
            "issuer_identity_digest": DIGESTS[9],
            "verifier_identity_digest": DIGESTS[0],
        }
        for name, changed in changes.items():
            with self.subTest(field=name):
                self.assertNotEqual(
                    replace(original, **{name: changed}).content_digest,
                    original.content_digest,
                )

    def test_testnet_qualification_cannot_alias_demo(self):
        testnet = qualification(provider_scope=scope("TESTNET"))
        demo = qualification(provider_scope=scope("DEMO"))
        self.assertNotEqual(testnet.content_digest, demo.content_digest)

    def test_route_semantics_are_part_of_q_not_external_boolean(self):
        q1 = qualification(route_semantics_digest=DIGESTS[3])
        q2 = qualification(route_semantics_digest=DIGESTS[4])
        self.assertNotEqual(q1.content_digest, q2.content_digest)

    def test_noncanonical_source_digest_and_versions_fail_closed(self):
        with self.assertRaises(ProviderQualificationIdentityError):
            qualification(adapter_source_git_sha="A" * 40)
        with self.assertRaises(ProviderQualificationIdentityError):
            qualification(adapter_source_git_sha="a" * 39)
        with self.assertRaises(ProviderQualificationIdentityError):
            qualification(campaign_version=True)
        with self.assertRaises(ProviderQualificationIdentityError):
            qualification(attestation_digest="7" * 64)

    def test_provider_scope_subclass_is_not_qualification_scope_authority(self):
        class HostileScope(ProviderFinancialScope):
            pass

        hostile = HostileScope(
            provider_id="BYBIT",
            runtime_environment="PAPER",
            provider_environment="TESTNET",
            entity_policy_id="LINEAR_ORDER_V1",
        )
        with self.assertRaisesRegex(
            ProviderQualificationIdentityError,
            "exact ProviderFinancialScope",
        ):
            qualification(provider_scope=hostile)


if __name__ == "__main__":
    unittest.main()
