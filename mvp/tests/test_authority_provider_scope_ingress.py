import unittest

import mvp.autotrade_mvp.authority as authority_module
from mvp.autotrade_mvp.authority import (
    AuthoritativeRiskSnapshot,
    InstrumentVersionIdentity,
    RiskAuthorityRequest,
)
from mvp.autotrade_mvp.risk import RiskContext, RiskIntent, RiskPolicy


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"


class _ExplosiveText(str):
    calls = 0

    def _explode(self, *_args, **_kwargs):
        type(self).calls += 1
        raise AssertionError("polymorphic text callback executed")

    strip = _explode
    upper = _explode
    lower = _explode
    __eq__ = _explode


class _ExplosiveDict(dict):
    calls = 0

    def get(self, *_args, **_kwargs):
        type(self).calls += 1
        raise AssertionError("polymorphic mapping callback executed")


def _intent():
    return RiskIntent.create(
        symbol="ABC",
        side="BUY",
        quantity="1",
        price="100",
        expected_state_version=1,
    )


def _context():
    return RiskContext.create(
        state_version=1,
        equity="1000",
        positions={},
        marks={"ABC": "100"},
        reserved_position_delta={},
        daily_pnl="0",
        drawdown_fraction="0",
        market_data_age_seconds="1",
        fx_age_seconds={},
        fx_required=False,
        margin_headroom="1",
        capability_allowed=True,
        borrow_available=None,
        stress_scenarios=(),
    )


def _policy():
    return RiskPolicy.create(
        max_abs_position="10",
        max_single_notional="1000",
        max_gross_leverage="2",
        max_net_leverage="2",
        max_daily_loss="500",
        max_drawdown_fraction="0.20",
        max_data_age_seconds="5",
        max_fx_age_seconds="60",
        min_margin_headroom="0.20",
        max_stress_loss="500",
    )


def _request_values():
    return {
        "risk_intent": _intent(),
        "account_id": "account-1",
        "environment": "SIMULATION",
        "provider_id": "TEST_PROVIDER",
        "instrument_version": InstrumentVersionIdentity(INSTRUMENT_ID, 1),
        "capability_snapshot_id": "capability-1",
        "reconciliation_checkpoint_event_id": "reconciliation-1",
        "journal_sequence_cut": 0,
        "reservation_version": 0,
        "reservation_state_digest": "reservation-digest",
        "authority_policy_id": "authority-policy",
        "authority_policy_version": 1,
        "evaluated_at": "2026-10-06T09:00:00Z",
    }


def _snapshot_values():
    return {
        "context": _context(),
        "risk_policy": _policy(),
        "account_id": "account-1",
        "environment": "SIMULATION",
        "provider_id": "TEST_PROVIDER",
        "instrument_version": InstrumentVersionIdentity(INSTRUMENT_ID, 1),
        "capability_snapshot_id": "capability-1",
        "reconciliation_checkpoint_event_id": "reconciliation-1",
        "journal_sequence_cut": 0,
        "reservation_version": 0,
        "reservation_state_digest": "reservation-digest",
        "authority_policy_id": "authority-policy",
        "authority_policy_version": 1,
        "evaluated_at": "2026-10-06T09:00:00Z",
        "valid_until": "2026-10-06T09:05:00Z",
        "evidence_refs": {
            "PORTFOLIO": "test:portfolio",
            "MARKET": "test:market",
            "MARGIN": "test:margin",
            "POLICY": "test:policy",
            "RECONCILIATION": "test:reconciliation",
            "CAPABILITY": "test:capability",
        },
    }


def _capital_payload():
    return {
        "schema_version": "settlement-capital-cut.v1",
        "journal_sequence": 1,
        "provider_id": "BYBIT",
        "account_id": "account-1",
        "environment": "PAPER",
        "provider_environment": "TESTNET",
        "settlement_scope_id": "settlement:testnet",
        "economic_book_id": "economic:testnet",
        "resources": {
            "CASH:USD": {
                "provider_available": "100",
                "local_available": "80",
                "effective_available": "80",
            }
        },
    }


class ProviderScopeIngressTests(unittest.TestCase):
    def setUp(self):
        _ExplosiveText.calls = 0
        _ExplosiveDict.calls = 0

    def test_risk_authority_request_rejects_polymorphic_authority_text_without_callbacks(self):
        for field in (
            "account_id",
            "environment",
            "provider_id",
            "capability_snapshot_id",
            "reconciliation_checkpoint_event_id",
            "reservation_state_digest",
            "authority_policy_id",
            "evaluated_at",
        ):
            with self.subTest(field=field):
                values = _request_values()
                values[field] = _ExplosiveText(str(values[field]))
                with self.assertRaisesRegex(TypeError, "must be exact text"):
                    RiskAuthorityRequest(**values)
                self.assertEqual(_ExplosiveText.calls, 0)

    def test_authoritative_snapshot_rejects_polymorphic_authority_text_without_callbacks(self):
        for field in (
            "account_id",
            "environment",
            "provider_id",
            "capability_snapshot_id",
            "reconciliation_checkpoint_event_id",
            "reservation_state_digest",
            "authority_policy_id",
            "evaluated_at",
            "valid_until",
        ):
            with self.subTest(field=field):
                values = _snapshot_values()
                values[field] = _ExplosiveText(str(values[field]))
                with self.assertRaisesRegex(TypeError, "must be exact text"):
                    AuthoritativeRiskSnapshot(**values)
                self.assertEqual(_ExplosiveText.calls, 0)

    def test_provider_scope_parser_rejects_polymorphic_text_before_virtual_dispatch(self):
        snapshot = {
            "provider_id": "BYBIT",
            "account_id": "account-1",
            "environment": "PAPER",
            "provider_environment": _ExplosiveText("TESTNET"),
        }
        with self.assertRaisesRegex(TypeError, "must be exact text"):
            authority_module._authoritative_risk_provider_scope(snapshot)
        self.assertEqual(_ExplosiveText.calls, 0)

    def test_provider_scope_matcher_rejects_mapping_subclasses_before_get(self):
        snapshot = {
            "provider_id": "BYBIT",
            "account_id": "account-1",
            "environment": "PAPER",
            "provider_environment": "TESTNET",
        }
        evidence = _ExplosiveDict(snapshot)
        with self.assertRaisesRegex(
            authority_module.AuthorityConflict,
            "test evidence is malformed",
        ):
            authority_module._require_provider_scope_matches_authoritative_risk_snapshot(
                snapshot,
                evidence,
                evidence_name="test evidence",
            )
        self.assertEqual(_ExplosiveDict.calls, 0)

    def test_provider_scope_parser_rejects_mapping_subclasses_before_get(self):
        snapshot = _ExplosiveDict(
            {
                "provider_id": "BYBIT",
                "account_id": "account-1",
                "environment": "PAPER",
                "provider_environment": "TESTNET",
            }
        )
        with self.assertRaisesRegex(
            authority_module.AuthorityConflict,
            "authoritative risk snapshot is malformed",
        ):
            authority_module._authoritative_risk_provider_scope(snapshot)
        self.assertEqual(_ExplosiveDict.calls, 0)


    def test_settlement_capital_rejects_polymorphic_top_level_mapping_before_callbacks(self):
        payload = _ExplosiveDict(_capital_payload())
        with self.assertRaisesRegex(
            authority_module.AuthorityConflict,
            "settlement capital adjustment is malformed",
        ):
            authority_module._canonical_settlement_capital_adjustment(
                payload,
                provider_available={"CASH:USD": "100"},
                required_resources=("CASH:USD",),
                provider_id="BYBIT",
                account_id="account-1",
                environment="PAPER",
                provider_environment="TESTNET",
            )
        self.assertEqual(_ExplosiveDict.calls, 0)

    def test_settlement_capital_rejects_polymorphic_nested_mappings_before_callbacks(self):
        cases = ("resources", "resource_adjustment", "provider_available")
        for case in cases:
            with self.subTest(case=case):
                _ExplosiveDict.calls = 0
                payload = _capital_payload()
                provider_available = {"CASH:USD": "100"}
                if case == "resources":
                    payload["resources"] = _ExplosiveDict(payload["resources"])
                elif case == "resource_adjustment":
                    payload["resources"]["CASH:USD"] = _ExplosiveDict(
                        payload["resources"]["CASH:USD"]
                    )
                else:
                    provider_available = _ExplosiveDict(provider_available)
                with self.assertRaises(authority_module.AuthorityConflict):
                    authority_module._canonical_settlement_capital_adjustment(
                        payload,
                        provider_available=provider_available,
                        required_resources=("CASH:USD",),
                        provider_id="BYBIT",
                        account_id="account-1",
                        environment="PAPER",
                        provider_environment="TESTNET",
                    )
                self.assertEqual(_ExplosiveDict.calls, 0)

    def test_settlement_capital_rejects_polymorphic_scope_text_before_callbacks(self):
        for field in (
            "provider_id",
            "account_id",
            "environment",
            "provider_environment",
            "settlement_scope_id",
            "economic_book_id",
        ):
            with self.subTest(field=field):
                _ExplosiveText.calls = 0
                payload = _capital_payload()
                payload[field] = _ExplosiveText(str(payload[field]))
                with self.assertRaisesRegex(TypeError, "must be exact text"):
                    authority_module._canonical_settlement_capital_adjustment(
                        payload,
                        provider_available={"CASH:USD": "100"},
                        required_resources=("CASH:USD",),
                        provider_id="BYBIT",
                        account_id="account-1",
                        environment="PAPER",
                        provider_environment="TESTNET",
                    )
                self.assertEqual(_ExplosiveText.calls, 0)

    def test_settlement_capital_rejects_polymorphic_numeric_text_before_callbacks(self):
        payload = _capital_payload()
        payload["resources"]["CASH:USD"]["provider_available"] = _ExplosiveText(
            "100"
        )
        with self.assertRaisesRegex(
            TypeError,
            "must use exact Decimal, string or integer input",
        ):
            authority_module._canonical_settlement_capital_adjustment(
                payload,
                provider_available={"CASH:USD": "100"},
                required_resources=("CASH:USD",),
                provider_id="BYBIT",
                account_id="account-1",
                environment="PAPER",
                provider_environment="TESTNET",
            )
        self.assertEqual(_ExplosiveText.calls, 0)

    def test_settlement_capital_accepts_exact_canonical_provider_domain(self):
        canonical, effective = (
            authority_module._canonical_settlement_capital_adjustment(
                _capital_payload(),
                provider_available={"CASH:USD": "100"},
                required_resources=("CASH:USD",),
                provider_id="BYBIT",
                account_id="account-1",
                environment="PAPER",
                provider_environment="TESTNET",
            )
        )
        self.assertEqual(canonical["provider_environment"], "TESTNET")
        self.assertEqual(canonical["resources"]["CASH:USD"]["effective_available"], "80")
        self.assertEqual(str(effective["CASH:USD"]), "80")



if __name__ == "__main__":
    unittest.main()
