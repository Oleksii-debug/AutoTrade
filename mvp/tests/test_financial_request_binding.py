from dataclasses import replace
import unittest

from mvp.autotrade_mvp.financial_request_binding import (
    FinancialRequestBindingError,
    FinancialRequestBindingMaterial,
)


D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
D3 = "sha256:" + "3" * 64
D4 = "sha256:" + "4" * 64
D5 = "sha256:" + "5" * 64
D6 = "sha256:" + "6" * 64
D7 = "sha256:" + "7" * 64
D8 = "sha256:" + "8" * 64
D9 = "sha256:" + "9" * 64
DA = "sha256:" + "a" * 64
DB = "sha256:" + "b" * 64
DC = "sha256:" + "c" * 64
RS1 = "risk-snapshot:sha256:" + "a" * 64
RS2 = "risk-snapshot:sha256:" + "c" * 64
RD1 = "risk:sha256:" + "b" * 64
RD2 = "risk:sha256:" + "d" * 64
AC1 = "provider-account-cut:sha256:" + "c" * 64
AC2 = "provider-account-cut:sha256:" + "d" * 64
PS1 = "provider-financial-scope:sha256:" + "3" * 64
PS2 = "provider-financial-scope:sha256:" + "c" * 64
Q1 = "provider-qualification:sha256:" + "5" * 64
Q2 = "provider-qualification:sha256:" + "c" * 64


def binding() -> FinancialRequestBindingMaterial:
    return FinancialRequestBindingMaterial(
        risk_snapshot_id=RS1,
        risk_decision_id=RD1,
        admitted_journal_sequence_cut=41,
        account_cut_id=AC1,
        account_cut_digest=DC,
        account_head_journal_sequence=37,
        reservation_id="reservation-1",
        reservation_scope_digest=D1,
        reservation_version=3,
        reservation_state_digest=D2,
        provider_scope_digest=PS1,
        provider_id="BYBIT",
        account_id="account-1",
        runtime_environment="PAPER",
        provider_environment="TESTNET",
        entity_policy_id="linear-order-v1",
        instrument_id="00000000-0000-0000-0000-000000000101",
        instrument_version=7,
        quantity_unit="CONTRACT",
        equivalent_exposure_digest=D4,
        capability_snapshot_id="capability-1",
        qualification_identity_digest=Q1,
        client_order_id="client-order-1",
        side="BUY",
        quantity="2",
        price="30000",
        price_semantics_digest=D6,
        order_type="LIMIT",
        time_in_force="GTC",
        reduce_only=False,
        trigger_protection_digest=D7,
        endpoint="/v5/order/create",
        query_sha256=D8,
        body_sha256=D9,
        request_sha256=DA,
        submission_scope_digest=DB,
    )


class FinancialRequestBindingTests(unittest.TestCase):
    def test_payload_is_versioned_and_binding_id_is_content_derived(self):
        value = binding()
        payload = value.payload()

        self.assertEqual(payload["schema_version"], "1.0.0")
        self.assertEqual(value.binding_id, binding().binding_id)
        self.assertTrue(value.binding_id.startswith("financial-request:sha256:"))

    def test_every_financial_or_wire_dimension_changes_binding_identity(self):
        original = binding()
        changes = {
            "risk_snapshot_id": RS2,
            "risk_decision_id": RD2,
            "admitted_journal_sequence_cut": 42,
            "account_cut_id": AC2,
            "account_cut_digest": D1,
            "account_head_journal_sequence": 38,
            "reservation_id": "reservation-2",
            "reservation_scope_digest": DC,
            "reservation_version": 4,
            "reservation_state_digest": DC,
            "provider_scope_digest": PS2,
            "provider_id": "KRAKEN",
            "account_id": "account-2",
            "provider_environment": "DEMO",
            "entity_policy_id": "inverse-order-v1",
            "instrument_id": "00000000-0000-0000-0000-000000000102",
            "instrument_version": 8,
            "quantity_unit": "BASE",
            "equivalent_exposure_digest": DC,
            "capability_snapshot_id": "capability-2",
            "qualification_identity_digest": Q2,
            "client_order_id": "client-order-2",
            "side": "SELL",
            "quantity": "3",
            "price": "30001",
            "price_semantics_digest": DC,
            "order_type": "MARKET",
            "time_in_force": "IOC",
            "reduce_only": True,
            "trigger_protection_digest": DC,
            "endpoint": "/v5/order/amend",
            "query_sha256": DC,
            "body_sha256": DC,
            "request_sha256": DC,
            "submission_scope_digest": DC,
        }
        for field_name, changed_value in changes.items():
            with self.subTest(field=field_name):
                changed = replace(original, **{field_name: changed_value})
                self.assertNotEqual(changed.binding_id, original.binding_id)

    def test_canonical_upstream_authority_identities_are_retained_without_namespace_loss(self):
        value = binding()
        self.assertEqual(value.risk_snapshot_id, RS1)
        self.assertEqual(value.risk_decision_id, RD1)
        self.assertEqual(value.account_cut_id, AC1)
        self.assertEqual(value.provider_scope_digest, PS1)
        self.assertEqual(value.qualification_identity_digest, Q1)

        invalid_cases = (
            ("risk_snapshot_id", RD1, "risk-snapshot:sha256"),
            ("risk_decision_id", RS1, "risk:sha256"),
            ("risk_snapshot_id", DA, "risk-snapshot:sha256"),
            ("risk_decision_id", DB, "risk:sha256"),
            ("account_cut_id", DC, "provider-account-cut:sha256"),
            ("provider_scope_digest", D3, "provider-financial-scope:sha256"),
            ("qualification_identity_digest", D5, "provider-qualification:sha256"),
            ("account_cut_id", PS1, "provider-account-cut:sha256"),
            ("provider_scope_digest", Q1, "provider-financial-scope:sha256"),
            ("qualification_identity_digest", AC1, "provider-qualification:sha256"),
        )
        for field_name, invalid, expected in invalid_cases:
            with self.subTest(field=field_name, invalid=invalid):
                with self.assertRaisesRegex(FinancialRequestBindingError, expected):
                    replace(value, **{field_name: invalid})

    def test_superseded_account_head_changes_financial_request_identity(self):
        admitted = binding()
        newer_head = replace(
            admitted,
            account_cut_id=AC2,
            account_cut_digest=D1,
            account_head_journal_sequence=38,
        )
        self.assertNotEqual(newer_head.binding_id, admitted.binding_id)

    def test_account_head_cannot_claim_state_newer_than_admission_cut(self):
        with self.assertRaisesRegex(
            FinancialRequestBindingError,
            "account head cannot be newer",
        ):
            replace(binding(), account_head_journal_sequence=42)

    def test_runtime_environment_is_part_of_identity(self):
        paper = binding()
        live = replace(paper, runtime_environment="LIVE")
        self.assertNotEqual(live.binding_id, paper.binding_id)

    def test_noncanonical_numeric_and_digest_inputs_fail_closed(self):
        with self.assertRaises(FinancialRequestBindingError):
            replace(binding(), quantity="02")
        with self.assertRaises(FinancialRequestBindingError):
            replace(binding(), price="3.0e4")
        with self.assertRaises(FinancialRequestBindingError):
            replace(binding(), request_sha256="a" * 64)
        with self.assertRaises(FinancialRequestBindingError):
            replace(binding(), account_cut_digest="c" * 64)
        with self.assertRaises(FinancialRequestBindingError):
            replace(binding(), qualification_identity_digest="provider-qualification:sha256:" + "A" * 64)
        with self.assertRaises(FinancialRequestBindingError):
            replace(binding(), risk_snapshot_id="risk-snapshot:sha256:" + "A" * 64)
        with self.assertRaises(FinancialRequestBindingError):
            replace(binding(), instrument_version=True)
        with self.assertRaises(FinancialRequestBindingError):
            replace(binding(), reduce_only=1)

    def test_provider_domain_account_head_and_exact_request_are_all_required(self):
        value = binding().payload()
        required = {
            "risk_snapshot_id",
            "risk_decision_id",
            "account_cut_id",
            "account_cut_digest",
            "account_head_journal_sequence",
            "provider_scope_digest",
            "provider_id",
            "account_id",
            "runtime_environment",
            "provider_environment",
            "entity_policy_id",
            "capability_snapshot_id",
            "qualification_identity_digest",
            "request_sha256",
            "submission_scope_digest",
        }
        self.assertFalse(required - set(value))


if __name__ == "__main__":
    unittest.main()
