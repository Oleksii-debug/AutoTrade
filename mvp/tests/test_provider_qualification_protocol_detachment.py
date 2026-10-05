from __future__ import annotations

import unittest

import mvp.autotrade_mvp.provider_qualification_authority as provider_authority

from mvp.autotrade_mvp.provider_qualification_authority import (
    ProviderQualificationProtocol,
    source_provider_qualification_protocol,
)


class ProviderQualificationProtocolDetachmentTests(unittest.TestCase):
    def test_module_global_decoys_cannot_retarget_source_protocol(self):
        attacker = ProviderQualificationProtocol(
            key="PROVIDER_ROUTE_V1",
            domain="ATTACKER",
            gate="ATTACKER_GATE",
            package_id="ATTACKER",
            protocol_id="attacker-route",
            protocol_version="9.9.9",
            requirement_id="attacker-required",
            campaign_evidence_kind="ATTACKER_EVIDENCE",
        )
        provider_authority._PROVIDER_ROUTE_V1 = attacker
        provider_authority._SOURCE_PROTOCOLS = {"PROVIDER_ROUTE_V1": attacker}
        try:
            resolved = source_provider_qualification_protocol("PROVIDER_ROUTE_V1")
        finally:
            del provider_authority._PROVIDER_ROUTE_V1
            del provider_authority._SOURCE_PROTOCOLS

        self.assertEqual(resolved.domain, "PROVIDER")
        self.assertEqual(resolved.gate, "ROUTE_QUALIFICATION")
        self.assertEqual(resolved.package_id, "AUTOTRADE")
        self.assertEqual(resolved.protocol_id, "provider-route-v1")
        self.assertEqual(resolved.protocol_version, "1.0.0")
        self.assertEqual(resolved.requirement_id, "provider-route-required")
        self.assertEqual(
            resolved.campaign_evidence_kind,
            "PROVIDER_QUALIFICATION_CAMPAIGN",
        )

    def test_public_source_protocol_is_detached_from_retained_authority(self):
        first = source_provider_qualification_protocol("PROVIDER_ROUTE_V1")
        second = source_provider_qualification_protocol("PROVIDER_ROUTE_V1")

        self.assertIs(type(first), ProviderQualificationProtocol)
        self.assertIs(type(second), ProviderQualificationProtocol)
        self.assertIsNot(first, second)
        self.assertEqual(first, second)

        # frozen=True does not make a Python object immutable to a caller that
        # deliberately uses object.__setattr__. A public result must therefore
        # never be the retained source-authority instance.
        object.__setattr__(first, "domain", "ATTACKER")
        object.__setattr__(first, "campaign_evidence_kind", "ATTACKER_EVIDENCE")

        resolved = source_provider_qualification_protocol("PROVIDER_ROUTE_V1")
        self.assertIsNot(resolved, first)
        self.assertEqual(resolved.domain, "PROVIDER")
        self.assertEqual(
            resolved.campaign_evidence_kind,
            "PROVIDER_QUALIFICATION_CAMPAIGN",
        )
        self.assertEqual(resolved.protocol_id, "provider-route-v1")
        self.assertEqual(resolved.protocol_version, "1.0.0")
        self.assertEqual(resolved.requirement_id, "provider-route-required")


if __name__ == "__main__":
    unittest.main()
