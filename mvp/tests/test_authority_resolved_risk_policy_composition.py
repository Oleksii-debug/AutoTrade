from dataclasses import fields
from inspect import getsource
import unittest

from mvp.autotrade_mvp.authority import (
    AuthoritativeRiskSnapshot,
    RiskAuthorityRequest,
)


class AuthorityResolvedRiskPolicyCompositionTests(unittest.TestCase):
    def test_financial_request_and_snapshot_retain_one_resolved_policy_authority(self):
        request_fields = {field.name: field for field in fields(RiskAuthorityRequest)}
        snapshot_fields = {
            field.name: field for field in fields(AuthoritativeRiskSnapshot)
        }

        self.assertIn(
            "resolved_risk_policy",
            request_fields,
            "financial request must retain registry-issued quantitative policy authority",
        )
        self.assertIn(
            "resolved_risk_policy",
            snapshot_fields,
            "authoritative snapshot must retain the same durable policy authority",
        )
        self.assertIn(
            "ResolvedRiskPolicy",
            str(request_fields["resolved_risk_policy"].type),
        )
        self.assertIn(
            "ResolvedRiskPolicy",
            str(snapshot_fields["resolved_risk_policy"].type),
        )

    def test_request_and_snapshot_revalidate_registry_issuance(self):
        request_init = getsource(RiskAuthorityRequest.__post_init__)
        snapshot_init = getsource(AuthoritativeRiskSnapshot.__post_init__)

        self.assertIn("require_registry_issued_resolved_policy", request_init)
        self.assertIn("require_registry_issued_resolved_policy", snapshot_init)

    def test_snapshot_identity_binds_durable_policy_activation_history(self):
        identity_source = getsource(AuthoritativeRiskSnapshot._identity_payload)

        self.assertIn("resolved_risk_policy", identity_source)
        self.assertIn("evidence_payload", identity_source)
        self.assertIn(
            "activation_event_id",
            identity_source,
            "same RiskPolicy values under another activation episode must be a different snapshot",
        )
        self.assertIn("registration_event_id", identity_source)
        self.assertIn("resolved_journal_sequence_cut", identity_source)
        self.assertIn("journal_store_identity_digest", identity_source)

    def test_service_cross_binds_request_and_snapshot_policy_authority(self):
        resolver_source = getsource(
            RiskAuthorityRequest.__module__
            and __import__(
                RiskAuthorityRequest.__module__,
                fromlist=["AuthorityService"],
            ).AuthorityService._resolve_authoritative_risk_snapshot
        )
        self.assertIn("resolved_risk_policy", resolver_source)


if __name__ == "__main__":
    unittest.main()
