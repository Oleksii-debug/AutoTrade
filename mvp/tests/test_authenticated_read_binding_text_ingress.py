from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    Surface,
    _canonical_query_values,
    prepare_authenticated_read_query,
)
from mvp.tests.capability_test_support import fresh_test_admission


NOW = datetime(2026, 10, 6, 0, tzinfo=timezone.utc)


class _HostileString(str):
    def strip(self, *args, **kwargs):
        raise AssertionError("hostile string method must never execute")


def capability():
    observed = NOW - timedelta(minutes=2)
    expires = NOW + timedelta(minutes=10)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BYBIT",
            account_id="test-account",
            entity_id="test-entity",
            environment="PAPER",
            provider_environment="TESTNET",
            instrument_version="95555555-5555-4555-8555-555555555555@1",
            observed_at=observed,
            expires_at=expires,
            supported_order_types=frozenset({"LIMIT"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ACCOUNT.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="text-ingress-test-v1",
            data_entitlements=frozenset({"ACTIVITIES"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "a" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return fresh_test_admission(
        derive_capability_snapshot(
            snapshot_id=str(uuid4()),
            claims=claims,
            observed_at=NOW,
            evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
        )
    )


class AuthenticatedReadTextIngressTests(unittest.TestCase):
    def test_query_key_string_subclass_is_rejected_before_virtual_strip(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "authenticated-read query keys must be canonical strings",
        ):
            _canonical_query_values(
                {_HostileString("category"): "option"}
            )

    def test_endpoint_string_subclass_is_rejected_before_virtual_strip(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "authenticated-read endpoint must be a canonical string",
        ):
            prepare_authenticated_read_query(
                capability=capability(),
                surface=Surface.ACTIVITIES,
                endpoint=_HostileString("/v5/account/transaction-log"),
                query={"category": "option"},
                at=NOW,
                permission_scope="ACCOUNT.READ",
            )

    def test_permission_scope_string_subclass_is_rejected_before_virtual_strip(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "permission_scope must be a canonical string",
        ):
            prepare_authenticated_read_query(
                capability=capability(),
                surface=Surface.ACTIVITIES,
                endpoint="/v5/account/transaction-log",
                query={"category": "option"},
                at=NOW,
                permission_scope=_HostileString("ACCOUNT.READ"),
            )


if __name__ == "__main__":
    unittest.main()
