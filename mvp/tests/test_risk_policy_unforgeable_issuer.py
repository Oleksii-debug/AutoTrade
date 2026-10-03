from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import risk_policy_authority as authority
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk import RiskPolicy
from mvp.autotrade_mvp.risk_policy_authority import (
    DurableRiskPolicyRegistry,
    RiskPolicyAuthorityError,
    RiskPolicyScope,
)


NOW = datetime(2026, 9, 30, 2, 30, tzinfo=timezone.utc)


def _scope() -> RiskPolicyScope:
    return RiskPolicyScope(
        provider_id="BYBIT",
        account_id="account-1",
        environment="PAPER",
        provider_environment="TESTNET",
        entity_policy_id="bybit-global-v1",
        instrument_family="PERPETUAL",
    )


def _policy() -> RiskPolicy:
    return RiskPolicy.create(
        max_abs_position="100",
        max_single_notional="1000",
        max_gross_leverage="2",
        max_net_leverage="1.5",
        max_daily_loss="100",
        max_drawdown_fraction="0.20",
        max_data_age_seconds="5",
        max_fx_age_seconds="5",
        min_margin_headroom="0.10",
        max_stress_loss="200",
        max_asset_concentration_fraction="0.75",
        max_venue_concentration_fraction="0.80",
        max_order_participation_fraction="0.10",
        max_spread_fraction="0.01",
        max_slippage_fraction="0.02",
        max_clock_age_seconds="2",
        allowed_actions=("TRADE", "REDUCE", "HEDGE", "FLATTEN"),
        require_settlement_evidence=True,
    )


class RiskPolicyIssuerUnforgeabilityTests(unittest.TestCase):
    @staticmethod
    def _issued(directory: str):
        store = JournalStore(Path(directory) / "journal.sqlite3")
        registry = DurableRiskPolicyRegistry(store)
        scope = _scope()
        registry.register(
            scope=scope,
            policy_id="core-risk",
            version=1,
            policy=_policy(),
            committed_at=NOW,
        )
        registry.activate(
            scope=scope,
            policy_id="core-risk",
            version=1,
            committed_at=NOW + timedelta(seconds=1),
        )
        return registry.resolve_current(scope)

    @staticmethod
    def _forge_from(issued):
        return authority.ResolvedRiskPolicy(
            identity=issued.identity,
            policy=issued.policy,
            registration_event_id=issued.registration_event_id,
            registration_journal_sequence=issued.registration_journal_sequence,
            activation_event_id=issued.activation_event_id,
            activation_journal_sequence=issued.activation_journal_sequence,
            resolved_journal_sequence_cut=issued.resolved_journal_sequence_cut,
            journal_store_identity_digest=issued.journal_store_identity_digest,
            _authority_token=authority._RESOLVED_POLICY_AUTHORITY_TOKEN,
        )

    def test_imported_module_token_cannot_mint_registry_accepted_policy(self):
        with TemporaryDirectory() as directory:
            issued = self._issued(directory)

            # The issuer capability must not be a caller-retrievable module global.
            # If a caller can import the exact token and pass it back into the
            # public dataclass constructor, issuance provenance is forgeable even
            # when every copied financial field is otherwise canonical.
            forged = self._forge_from(issued)

            with self.assertRaises(RiskPolicyAuthorityError):
                authority.require_registry_issued_resolved_policy(forged)

    def test_imported_module_has_no_issuance_registration_capability(self):
        with TemporaryDirectory() as directory:
            issued = self._issued(directory)
            forged = self._forge_from(issued)

            # The registrar and mutable issuance table are closure-owned by the
            # genuine registry resolver, not caller-retrievable module globals.
            self.assertFalse(
                hasattr(authority, "_record_resolved_policy_issuance")
            )
            self.assertFalse(
                hasattr(authority, "_RESOLVED_POLICY_ISSUANCE_BINDINGS")
            )

            # Recomputing every caller-visible seal still cannot register the
            # forged object.
            object.__setattr__(
                forged,
                "_authority_digest",
                authority._resolved_policy_authority_digest(forged),
            )
            with self.assertRaisesRegex(
                RiskPolicyAuthorityError,
                "was not issued by DurableRiskPolicyRegistry",
            ):
                authority.require_registry_issued_resolved_policy(forged)

            self.assertIs(
                authority.require_registry_issued_resolved_policy(issued),
                issued,
            )


if __name__ == "__main__":
    unittest.main()
