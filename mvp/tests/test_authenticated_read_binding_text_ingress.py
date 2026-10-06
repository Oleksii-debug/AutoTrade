from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilitySnapshot,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    ProviderResponseObservation,
    Surface,
    _canonical_query_values,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
    provider_response_observation_projection,
)
from mvp.tests.capability_test_support import fresh_test_admission


NOW = datetime(2026, 10, 6, 0, tzinfo=timezone.utc)


class _HostileString(str):
    def strip(self, *args, **kwargs):
        raise AssertionError("hostile string method must never execute")


class _HostileSurface:
    def __str__(self):
        raise AssertionError("hostile surface stringification must never execute")


class _HostileDict(dict):
    items_called = False

    def items(self):
        type(self).items_called = True
        raise AssertionError("hostile mapping callback must never execute")


class _HostileDatetime(datetime):
    def utcoffset(self):
        raise AssertionError("hostile datetime callback must never execute")

    def astimezone(self, *args, **kwargs):
        raise AssertionError("hostile datetime callback must never execute")


class _HostileCapabilitySnapshot(CapabilitySnapshot):
    def __getattribute__(self, name):
        if name in {"status", "observed_at", "expires_at", "provider_id"}:
            raise AssertionError("hostile capability callback must never execute")
        return super().__getattribute__(name)


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
    def test_query_mapping_subclass_is_rejected_before_items_callback(self):
        _HostileDict.items_called = False
        with self.assertRaisesRegex(
            ProviderCoreError,
            "query must be an exact inert mapping",
        ):
            _canonical_query_values(_HostileDict({"category": "option"}))
        self.assertFalse(_HostileDict.items_called)

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

    def test_endpoint_whitespace_is_rejected_instead_of_normalized(self):
        with self.assertRaisesRegex(
            ProviderCoreError,
            "authenticated-read endpoint must be a canonical string",
        ):
            prepare_authenticated_read_query(
                capability=capability(),
                surface=Surface.ACTIVITIES,
                endpoint=" /v5/account/transaction-log",
                query={"category": "option"},
                at=NOW,
                permission_scope="ACCOUNT.READ",
            )

    def test_permission_scope_whitespace_is_rejected_instead_of_normalized(self):
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
                permission_scope=" ACCOUNT.READ",
            )

    def test_non_surface_is_rejected_before_stringification(self):
        with self.assertRaisesRegex(
            TypeError,
            "surface must be exact Surface",
        ):
            prepare_authenticated_read_query(
                capability=capability(),
                surface=_HostileSurface(),
                endpoint="/v5/account/transaction-log",
                query={"category": "option"},
                at=NOW,
                permission_scope="ACCOUNT.READ",
            )

    def test_datetime_subclass_is_rejected_before_time_callbacks(self):
        hostile = _HostileDatetime(2026, 10, 6, tzinfo=timezone.utc)
        with self.assertRaisesRegex(
            ProviderCoreError,
            "at must be an exact timezone-aware datetime",
        ):
            prepare_authenticated_read_query(
                capability=capability(),
                surface=Surface.ACTIVITIES,
                endpoint="/v5/account/transaction-log",
                query={"category": "option"},
                at=hostile,
                permission_scope="ACCOUNT.READ",
            )

    def test_authenticated_response_projection_ignores_require_scope_rebinding(self):
        binding = prepare_authenticated_read_query(
            capability=capability(),
            surface=Surface.ACTIVITIES,
            endpoint="/v5/account/transaction-log",
            query={"category": "option"},
            at=NOW,
            permission_scope="ACCOUNT.READ",
        )
        observation = observe_authenticated_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=b'{"result":{"list":[]}}',
            observed_at=NOW,
        )
        with patch.object(
            ProviderResponseObservation,
            "require_scope",
            lambda *_args, **_kwargs: None,
        ):
            projection = provider_response_observation_projection(observation)
        self.assertEqual(projection["provider_id"], "BYBIT")
        self.assertEqual(projection["endpoint"], "/v5/account/transaction-log")
        self.assertEqual(projection["permission_scope"], "ACCOUNT.READ")
        self.assertIs(projection["query_binding"], binding)
        self.assertIs(projection["payload"], observation.payload)

    def test_authenticated_response_projection_rejects_post_mint_response_retargeting(self):
        binding = prepare_authenticated_read_query(
            capability=capability(),
            surface=Surface.ACTIVITIES,
            endpoint="/v5/account/transaction-log",
            query={"category": "option"},
            at=NOW,
            permission_scope="ACCOUNT.READ",
        )
        observation = observe_authenticated_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=b'{"result":{"list":[]}}',
            observed_at=NOW,
        )
        object.__setattr__(
            observation,
            "evidence_ref",
            "provider-read:sha256:" + "0" * 64,
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "provider response changed after exact-byte observation",
        ):
            provider_response_observation_projection(observation)

    def test_authenticated_response_projection_rejects_post_mint_query_retargeting(self):
        binding = prepare_authenticated_read_query(
            capability=capability(),
            surface=Surface.ACTIVITIES,
            endpoint="/v5/account/transaction-log",
            query={"category": "option"},
            at=NOW,
            permission_scope="ACCOUNT.READ",
        )
        observation = observe_authenticated_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=b'{"result":{"list":[]}}',
            observed_at=NOW,
        )
        object.__setattr__(binding, "endpoint", "/v5/order/realtime")
        with self.assertRaisesRegex(
            ProviderCoreError,
            "authenticated-read binding changed after preparation",
        ):
            provider_response_observation_projection(observation)

    def test_capability_subclass_is_rejected_before_authority_reads(self):
        hostile = object.__new__(_HostileCapabilitySnapshot)
        with self.assertRaisesRegex(
            TypeError,
            "capability must be exact CapabilitySnapshot",
        ):
            prepare_authenticated_read_query(
                capability=hostile,
                surface=Surface.ACTIVITIES,
                endpoint="/v5/account/transaction-log",
                query={"category": "option"},
                at=NOW,
                permission_scope="ACCOUNT.READ",
            )


if __name__ == "__main__":
    unittest.main()
