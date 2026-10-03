from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import unittest

from mvp.autotrade_mvp.risk import (
    LiquidationHeadroomEvidence,
    LiquidationScope,
    _verify_liquidation_headroom_evidence,
    liquidation_evidence_payload,
)


class RiskLiquidationStoreAuthorityTests(unittest.TestCase):
    def test_structural_snapshot_reader_is_not_financial_evidence_authority(self):
        observed = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
        scope = LiquidationScope(
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
            margin_mode="CROSS",
            risk_tier_version="tier-v1",
        )
        provisional = LiquidationHeadroomEvidence.create(
            headroom=Decimal("0.50"),
            provider_id=scope.provider_id,
            account_id=scope.account_id,
            environment=scope.environment,
            margin_mode=scope.margin_mode,
            risk_tier_version=scope.risk_tier_version,
            state_version=7,
            observed_at=observed,
            expires_at=observed + timedelta(minutes=5),
            artifact_id="11111111-1111-4111-8111-111111111111",
            sha256="sha256:" + "0" * 64,
        )
        payload = liquidation_evidence_payload(provisional)
        raw = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = "sha256:" + sha256(raw).hexdigest()
        evidence = LiquidationHeadroomEvidence.create(
            headroom=provisional.headroom,
            provider_id=provisional.provider_id,
            account_id=provisional.account_id,
            environment=provisional.environment,
            margin_mode=provisional.margin_mode,
            risk_tier_version=provisional.risk_tier_version,
            state_version=provisional.state_version,
            observed_at=provisional.observed_at,
            expires_at=provisional.expires_at,
            artifact_id=provisional.artifact_id,
            sha256=digest,
        )
        payload = liquidation_evidence_payload(evidence)
        raw = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        class CallerOwnedSnapshotReader:
            def read_authenticated_snapshot(self, artifact_id):
                self.requested_artifact_id = artifact_id
                return (
                    {
                        "artifact_id": artifact_id,
                        "sha256": evidence.sha256,
                        "manifest_hash": "caller-asserted-integrity",
                        "metadata": payload,
                    },
                    raw,
                )

        caller_store = CallerOwnedSnapshotReader()
        self.assertFalse(
            _verify_liquidation_headroom_evidence(
                evidence=evidence,
                expected_scope=scope,
                expected_state_version=7,
                decision_time=observed + timedelta(seconds=1),
                evidence_store=caller_store,
            )
        )


if __name__ == "__main__":
    unittest.main()
