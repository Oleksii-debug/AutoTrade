import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.asset_provider_crosswalk as crosswalk_module
from mvp.autotrade_mvp.asset_provider_crosswalk import (
    CrosswalkError,
    CrosswalkKey,
    LifecycleEvidence,
    advertised_lifecycle_keys,
    qualify_asset_provider_crosswalk,
)
from mvp.tests.test_asset_provider_crosswalk import (
    SOURCE,
    adapter_map,
    complete_evidence,
    trusted_qualify,
)


class AssetProviderCrosswalkSnapshotTests(unittest.TestCase):
    def test_terminal_crosswalk_rejects_polymorphic_evidence(self):
        class HostileEvidence(LifecycleEvidence):
            pass

        value = complete_evidence(advertised_lifecycle_keys()[0])
        hostile = HostileEvidence(
            key=value.key,
            source_sha=value.source_sha,
            adapter_sha=value.adapter_sha,
            cases=value.cases,
            reconciliation_complete=value.reconciliation_complete,
            economic_units_exact=value.economic_units_exact,
            artifact_id=value.artifact_id,
            artifact_sha256=value.artifact_sha256,
        )

        with self.assertRaisesRegex(CrosswalkError, "exact LifecycleEvidence"):
            qualify_asset_provider_crosswalk(
                [hostile],
                exact_source_sha=SOURCE,
                exact_adapter_shas=adapter_map(),
            )

    def test_terminal_crosswalk_rejects_polymorphic_nested_key(self):
        class HostileKey(CrosswalkKey):
            pass

        value = complete_evidence(advertised_lifecycle_keys()[0])
        hostile_key = HostileKey(
            value.key.provider_id,
            value.key.product_family,
            value.key.lifecycle,
        )
        hostile = LifecycleEvidence(
            key=hostile_key,
            source_sha=value.source_sha,
            adapter_sha=value.adapter_sha,
            cases=value.cases,
            reconciliation_complete=value.reconciliation_complete,
            economic_units_exact=value.economic_units_exact,
            artifact_id=value.artifact_id,
            artifact_sha256=value.artifact_sha256,
        )

        with self.assertRaisesRegex(CrosswalkError, "exact CrosswalkKey"):
            qualify_asset_provider_crosswalk(
                [hostile],
                exact_source_sha=SOURCE,
                exact_adapter_shas=adapter_map(),
            )

    def test_terminal_crosswalk_rejects_dynamic_outer_authorities(self):
        evidence = [complete_evidence(advertised_lifecycle_keys()[0])]

        with self.assertRaisesRegex(CrosswalkError, "exact list or tuple"):
            qualify_asset_provider_crosswalk(
                (item for item in evidence),
                exact_source_sha=SOURCE,
                exact_adapter_shas=adapter_map(),
            )

        class AdapterMap(dict):
            pass

        with self.assertRaisesRegex(CrosswalkError, "exact dict"):
            qualify_asset_provider_crosswalk(
                evidence,
                exact_source_sha=SOURCE,
                exact_adapter_shas=AdapterMap(adapter_map()),
            )

    def test_verifier_side_mutation_cannot_change_detached_lifecycle_decision(self):
        evidence = [
            complete_evidence(key)
            for key in advertised_lifecycle_keys()
        ]
        original = evidence[0]
        real_verify = crosswalk_module.verify_canonical_qualification_attestation

        def mutate_then_verify(receipt, **kwargs):
            object.__setattr__(original, "reconciliation_complete", False)
            object.__setattr__(original, "economic_units_exact", False)
            object.__setattr__(original, "adapter_sha", "3" * 40)
            return real_verify(receipt, **kwargs)

        with patch.object(
            crosswalk_module,
            "verify_canonical_qualification_attestation",
            side_effect=mutate_then_verify,
        ):
            verdict = trusted_qualify(evidence)

        self.assertFalse(original.reconciliation_complete)
        self.assertFalse(original.economic_units_exact)
        self.assertEqual(original.adapter_sha, "3" * 40)
        self.assertEqual(verdict.status, "PASS")
        self.assertEqual(verdict.missing_keys, ())
        self.assertEqual(verdict.invalid_keys, ())
        self.assertEqual(verdict.reason_codes, ())


if __name__ == "__main__":
    unittest.main()
