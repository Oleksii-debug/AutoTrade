from dataclasses import fields
from inspect import getsource
import unittest

from mvp.autotrade_mvp.authority import (
    AuthoritativeRiskSnapshot,
    AuthorityService,
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

    def test_selected_provider_domain_dimensions_exist_on_both_sides_of_policy_cut(self):
        request_fields = {field.name for field in fields(RiskAuthorityRequest)}
        snapshot_fields = {field.name for field in fields(AuthoritativeRiskSnapshot)}
        required = {
            "provider_environment",
            "entity_policy_id",
            "instrument_family",
        }

        self.assertFalse(
            required - request_fields,
            "financial request needs exact selected provider-domain dimensions to cross-bind RiskPolicyScope",
        )
        self.assertFalse(
            required - snapshot_fields,
            "authoritative snapshot must retain the same policy/provider-domain scope",
        )

    def test_request_and_snapshot_revalidate_registry_issuance_and_scope(self):
        request_init = getsource(RiskAuthorityRequest.__post_init__)
        snapshot_init = getsource(AuthoritativeRiskSnapshot.__post_init__)

        self.assertIn("require_registry_issued_resolved_policy", request_init)
        self.assertIn("require_registry_issued_resolved_policy", snapshot_init)
        for source in (request_init, snapshot_init):
            self.assertIn("provider_environment", source)
            self.assertIn("entity_policy_id", source)
            self.assertIn("instrument_family", source)
            self.assertIn("identity.scope", source)

    def test_snapshot_identity_binds_durable_policy_activation_history_and_scope(self):
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
        for dimension in (
            "provider_environment",
            "entity_policy_id",
            "instrument_family",
        ):
            self.assertIn(dimension, identity_source)

    def test_service_cross_binds_request_and_snapshot_policy_authority_and_scope(self):
        resolver_source = getsource(
            AuthorityService._resolve_authoritative_risk_snapshot
        )
        self.assertIn("resolved_risk_policy", resolver_source)
        self.assertIn("provider_environment", resolver_source)
        self.assertIn("entity_policy_id", resolver_source)
        self.assertIn("instrument_family", resolver_source)


if __name__ == "__main__":
    unittest.main()
