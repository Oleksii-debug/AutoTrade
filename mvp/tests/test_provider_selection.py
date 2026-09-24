from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid5, NAMESPACE_URL

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.provider_core import (
    QualificationEvidence,
    REQUIRED_QUALIFICATION_CASES,
)
from mvp.autotrade_mvp.provider_selection import (
    ProviderCandidate,
    ProviderRouteRequest,
    select_provider,
)


NOW = datetime(2026, 9, 24, 18, tzinfo=timezone.utc)
INSTRUMENT = "instrument-v1"


def capability(provider: str, *, environment="PAPER", order_types=("LIMIT", "MARKET")):
    claims = []
    for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT"):
        artifact_id = str(uuid5(NAMESPACE_URL, f"{provider}:{source}:{environment}"))
        claims.append(
            CapabilityClaim(
                source=source,
                provider_id=provider,
                account_id=f"{provider.lower()}-account",
                entity_id="entity-1",
                environment=environment,
                instrument_version=INSTRUMENT,
                observed_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(hours=1),
                supported_order_types=frozenset(order_types),
                time_in_force=frozenset({"GTC", "IOC"}),
                permission_scopes=frozenset({"ORDER.WRITE", "ORDER.READ"}),
                position_mode="NET",
                native_protection=frozenset(),
                rate_limit_policy_id=f"{provider.lower()}-limits",
                data_entitlements=frozenset({"QUOTE", "TRADE"}),
                evidence_ref={
                    "artifact_id": artifact_id,
                    "sha256": "sha256:" + "a" * 64,
                    "observed_at": "2026-09-24T17:59:00Z",
                },
            )
        )
    return derive_capability_snapshot(
        snapshot_id=str(uuid5(NAMESPACE_URL, f"snapshot:{provider}:{environment}")),
        claims=claims,
        observed_at=NOW,
    )


def candidate(
    provider: str,
    family: str,
    *,
    environment="PAPER",
    code_sha=None,
    qualified_sha=None,
    unsupported=(),
):
    code = code_sha or "1" * 40
    evidence_sha = qualified_sha or code
    return ProviderCandidate(
        provider_id=provider,
        product_family=family,
        adapter_code_sha=code,
        qualification=QualificationEvidence(
            provider_id=provider,
            product_family=family,
            environment=environment,
            adapter_code_sha=evidence_sha,
            documentation_ref=f"official:{provider.lower()}:{family.lower()}",
            observed_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(days=1),
            passed_cases=REQUIRED_QUALIFICATION_CASES,
            unsupported_features=tuple(unsupported),
        ),
        capability=capability(provider, environment=environment),
    )


def request(**overrides):
    values = dict(
        asset_class="CRYPTO_SPOT",
        environment="PAPER",
        instrument_version=INSTRUMENT,
        order_type="LIMIT",
        time_in_force="GTC",
        permission_scope="ORDER.WRITE",
    )
    values.update(overrides)
    return ProviderRouteRequest(**values)


class ProviderSelectionTests(unittest.TestCase):
    def test_single_exact_candidate_is_selected(self):
        bybit = candidate("BYBIT", "SPOT")
        result = select_provider(request(), [bybit], at=NOW)
        self.assertEqual(result.status, "SELECTED_UNAMBIGUOUS")
        self.assertIs(result.selected, bybit)

    def test_multiple_eligible_providers_never_trigger_implicit_fallback(self):
        bybit = candidate("BYBIT", "SPOT")
        binance = candidate("BINANCE", "SPOT")
        result = select_provider(request(), [bybit, binance], at=NOW)
        self.assertEqual(result.status, "AMBIGUOUS_REQUIRES_POLICY")
        self.assertIsNone(result.selected)
        self.assertEqual(
            {(item.provider_id, item.product_family) for item in result.eligible},
            {("BYBIT", "SPOT"), ("BINANCE", "SPOT")},
        )

    def test_explicit_provider_policy_resolves_ambiguity_only_if_eligible(self):
        bybit = candidate("BYBIT", "SPOT")
        binance = candidate("BINANCE", "SPOT")
        result = select_provider(
            request(preferred_provider_id="binance"),
            [bybit, binance],
            at=NOW,
        )
        self.assertEqual(result.status, "SELECTED_BY_EXPLICIT_POLICY")
        self.assertEqual(result.selected.provider_id, "BINANCE")

        missing = select_provider(
            request(preferred_provider_id="kraken"),
            [bybit, binance],
            at=NOW,
        )
        self.assertEqual(missing.status, "NO_ELIGIBLE_PREFERRED_PROVIDER")
        self.assertIsNone(missing.selected)

    def test_asset_product_mismatch_is_rejected_even_with_valid_capability(self):
        bybit_spot = candidate("BYBIT", "SPOT")
        result = select_provider(
            request(asset_class="OPTION"),
            [bybit_spot],
            at=NOW,
        )
        self.assertEqual(result.status, "NO_ELIGIBLE_PROVIDER")
        self.assertIn("ASSET_PRODUCT_MISMATCH", result.decisions[0].reasons)

    def test_exact_adapter_code_is_required(self):
        stale = candidate(
            "BYBIT",
            "SPOT",
            code_sha="2" * 40,
            qualified_sha="3" * 40,
        )
        result = select_provider(request(), [stale], at=NOW)
        self.assertEqual(result.status, "NO_ELIGIBLE_PROVIDER")
        self.assertIn("QUALIFICATION_CODE_MISMATCH", result.decisions[0].reasons)

    def test_candidate_requires_actual_hex_code_sha_shape(self):
        with self.assertRaisesRegex(ValueError, "hex SHA"):
            candidate("BYBIT", "SPOT", code_sha="build-label")

    def test_live_never_inherits_nonlive_qualification(self):
        live = candidate("BYBIT", "SPOT", environment="LIVE")
        result = select_provider(
            request(environment="LIVE"),
            [live],
            at=NOW,
        )
        self.assertEqual(result.status, "NO_ELIGIBLE_PROVIDER")
        self.assertIn("LIVE_QUALIFICATION_NOT_ESTABLISHED", result.decisions[0].reasons)

    def test_explicit_unsupported_feature_blocks_route(self):
        bybit = candidate(
            "BYBIT",
            "SPOT",
            unsupported=("ORDER_TYPE:LIMIT",),
        )
        result = select_provider(request(), [bybit], at=NOW)
        self.assertEqual(result.status, "NO_ELIGIBLE_PROVIDER")
        self.assertIn(
            "QUALIFICATION_EXPLICITLY_UNSUPPORTED",
            result.decisions[0].reasons,
        )


if __name__ == "__main__":
    unittest.main()
