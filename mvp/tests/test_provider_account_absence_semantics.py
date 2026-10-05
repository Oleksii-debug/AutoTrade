from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.provider_account_absence_semantics import (
    ProviderAccountAbsenceSemanticsError,
    QualifiedProviderAccountAbsenceSemantics,
    account_reconciliation_absence_route_semantic,
    require_provider_account_absence_rule,
    require_provider_account_absence_semantics_authority,
    resolve_current_provider_account_absence_semantics,
)
from mvp.autotrade_mvp.provider_account_reconciliation_semantics import (
    account_reconciliation_route_semantics,
    resolve_current_provider_account_reconciliation_semantics,
)
from mvp.tests.test_provider_selection import NOW, ProviderSelectionTests


_RULES = {
    "OPEN_ORDERS": {
        "endpoint": "/v5/order/realtime",
        "data_entitlement": "ORDERS",
        "retention_rule_id": "BYBIT_OPEN_ORDER_RETENTION_V1",
    },
    "ORDER_HISTORY": {
        "endpoint": "/v5/order/history",
        "data_entitlement": "ORDERS",
        "retention_rule_id": "BYBIT_ORDER_HISTORY_RETENTION_V1",
    },
    "EXECUTIONS": {
        "endpoint": "/v5/execution/list",
        "data_entitlement": "EXECUTIONS",
        "retention_rule_id": "BYBIT_EXECUTION_RETENTION_V1",
    },
    "ACTIVITIES": {
        "endpoint": "/v5/account/transaction-log",
        "data_entitlement": "ACTIVITIES",
        "retention_rule_id": "BYBIT_ACTIVITY_RETENTION_V1",
    },
}


def rule_claim(surface: str, *, retention_rule_id: str | None = None) -> dict[str, str]:
    rule = _RULES[surface]
    return account_reconciliation_absence_route_semantic(
        surface=surface,
        endpoint=rule["endpoint"],
        data_entitlement=rule["data_entitlement"],
        query_scope_rule_id="BYBIT_SPOT_ACCOUNT_QUERY_V1",
        pagination_rule_id="BYBIT_V5_CURSOR_V1",
        retention_rule_id=retention_rule_id or rule["retention_rule_id"],
        consistency_horizon_rule_id="BYBIT_ACCOUNT_CONSISTENCY_HORIZON_V1",
        semantics_version=1,
    )


def all_claims(*, omit: str | None = None) -> dict[str, str]:
    claims = account_reconciliation_route_semantics(
        acquisition_mode="SERIALIZED_ACQUISITION_GENERATION",
        consistency_method_id="SERIALIZED_SNAPSHOT_READBACK",
        consistency_method_version=1,
    )
    for surface in _RULES:
        if surface != omit:
            claims.update(rule_claim(surface))
    return claims


class ProviderAccountAbsenceSemanticsTests(unittest.TestCase):
    def authorities(self, directory: str, *, claims=None):
        helper = ProviderSelectionTests(
            methodName="test_exact_current_c_and_q_are_selected_at_one_journal_cut"
        )
        self.addCleanup(helper.doCleanups)
        _capabilities, qualifications, record = helper.authorities(
            directory,
            extra_route_semantics=all_claims() if claims is None else claims,
        )
        reconciliation = resolve_current_provider_account_reconciliation_semantics(
            qualification_registry=qualifications,
            qualification_id=record.qualification_id,
            provider_scope_digest=record.scope.provider_scope.content_digest,
            at=NOW,
        )
        return qualifications, record, reconciliation

    def resolve(self, qualifications, reconciliation, *, at=NOW):
        return resolve_current_provider_account_absence_semantics(
            reconciliation_semantics=reconciliation,
            qualification_registry=qualifications,
            at=at,
        )

    def test_exact_current_q_issues_sealed_four_surface_absence_semantics(self):
        with TemporaryDirectory() as directory:
            qualifications, record, reconciliation = self.authorities(directory)
            value = self.resolve(qualifications, reconciliation)

            self.assertEqual(value.provider_scope_digest, record.scope.provider_scope.content_digest)
            self.assertEqual(value.qualification_id, record.qualification_id)
            self.assertEqual(
                value.qualification_route_semantics_digest,
                record.identity.route_semantics_digest,
            )
            self.assertEqual(
                value.reconciliation_semantics_digest,
                reconciliation.content_digest,
            )
            self.assertEqual(
                {rule["surface"] for rule in value.rules},
                set(_RULES),
            )
            self.assertTrue(
                value.content_digest.startswith(
                    "provider-account-absence-semantics:sha256:"
                )
            )
            require_provider_account_absence_semantics_authority(
                value,
                qualification_registry=qualifications,
                at=NOW,
            )

    def test_exact_rule_lookup_retains_endpoint_entitlement_and_retention_identity(self):
        with TemporaryDirectory() as directory:
            qualifications, _record, reconciliation = self.authorities(directory)
            value = self.resolve(qualifications, reconciliation)
            rule = require_provider_account_absence_rule(
                value,
                surface="EXECUTIONS",
                endpoint="/v5/execution/list",
                data_entitlement="EXECUTIONS",
                qualification_registry=qualifications,
                at=NOW,
            )
            self.assertEqual(rule["query_scope_rule_id"], "BYBIT_SPOT_ACCOUNT_QUERY_V1")
            self.assertEqual(rule["pagination_rule_id"], "BYBIT_V5_CURSOR_V1")
            self.assertEqual(rule["retention_rule_id"], "BYBIT_EXECUTION_RETENTION_V1")
            self.assertEqual(
                rule["consistency_horizon_rule_id"],
                "BYBIT_ACCOUNT_CONSISTENCY_HORIZON_V1",
            )
            self.assertEqual(rule["semantics_version"], 1)
            self.assertTrue(rule["rule_digest"].startswith("sha256:"))

    def test_missing_one_required_surface_rule_fails_closed(self):
        with TemporaryDirectory() as directory:
            qualifications, _record, reconciliation = self.authorities(
                directory,
                claims=all_claims(omit="ACTIVITIES"),
            )
            with self.assertRaisesRegex(
                ProviderAccountAbsenceSemanticsError,
                "lacks source-owned absence rules for: ACTIVITIES",
            ):
                self.resolve(qualifications, reconciliation)

    def test_constructor_cannot_forge_absence_semantics(self):
        with self.assertRaisesRegex(
            ProviderAccountAbsenceSemanticsError,
            "must come from exact current reconciliation Q",
        ):
            QualifiedProviderAccountAbsenceSemantics()

    def test_object_new_without_registration_has_no_authority(self):
        forged = object.__new__(QualifiedProviderAccountAbsenceSemantics)
        object.__setattr__(forged, "provider_scope_digest", "provider-financial-scope:sha256:" + "0" * 64)
        object.__setattr__(forged, "qualification_id", "provider-qualification:sha256:" + "0" * 64)
        object.__setattr__(forged, "qualification_route_semantics_digest", "sha256:" + "0" * 64)
        object.__setattr__(forged, "reconciliation_semantics_digest", "sha256:" + "0" * 64)
        object.__setattr__(forged, "rules_json", "[]")
        with self.assertRaisesRegex(
            ProviderAccountAbsenceSemanticsError,
            "construction authority is unavailable",
        ):
            require_provider_account_absence_semantics_authority(forged)

    def test_post_issue_mutation_is_detected(self):
        with TemporaryDirectory() as directory:
            qualifications, _record, reconciliation = self.authorities(directory)
            value = self.resolve(qualifications, reconciliation)
            object.__setattr__(value, "rules_json", "[]")
            with self.assertRaisesRegex(
                ProviderAccountAbsenceSemanticsError,
                "changed after Q resolution",
            ):
                require_provider_account_absence_semantics_authority(
                    value,
                    qualification_registry=qualifications,
                    at=NOW,
                )

    def test_cross_store_registry_cannot_reuse_absence_semantics(self):
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            first_registry, first_record, first_reconciliation = self.authorities(first)
            second_registry, second_record, _second_reconciliation = self.authorities(second)
            self.assertEqual(first_record.qualification_id, second_record.qualification_id)
            value = self.resolve(first_registry, first_reconciliation)
            with self.assertRaisesRegex(
                ProviderAccountAbsenceSemanticsError,
                "same exact JournalStore generation",
            ):
                require_provider_account_absence_semantics_authority(
                    value,
                    qualification_registry=second_registry,
                    at=NOW,
                )

    def test_expired_q_cannot_refresh_absence_semantics(self):
        with TemporaryDirectory() as directory:
            qualifications, _record, reconciliation = self.authorities(directory)
            with self.assertRaisesRegex(
                ProviderAccountAbsenceSemanticsError,
                "not exact current authority",
            ):
                self.resolve(
                    qualifications,
                    reconciliation,
                    at=datetime(2026, 10, 6, tzinfo=timezone.utc),
                )

    def test_issued_semantics_cannot_be_consumed_after_q_expiry(self):
        with TemporaryDirectory() as directory:
            qualifications, _record, reconciliation = self.authorities(directory)
            value = self.resolve(qualifications, reconciliation)
            with self.assertRaisesRegex(
                ProviderAccountAbsenceSemanticsError,
                "not exact current authority",
            ):
                require_provider_account_absence_rule(
                    value,
                    surface="EXECUTIONS",
                    endpoint="/v5/execution/list",
                    data_entitlement="EXECUTIONS",
                    qualification_registry=qualifications,
                    at=datetime(2026, 10, 6, tzinfo=timezone.utc),
                )

    def test_registry_bound_consumption_requires_explicit_time(self):
        with TemporaryDirectory() as directory:
            qualifications, _record, reconciliation = self.authorities(directory)
            value = self.resolve(qualifications, reconciliation)
            with self.assertRaisesRegex(
                ProviderAccountAbsenceSemanticsError,
                "exact consumption time",
            ):
                require_provider_account_absence_semantics_authority(
                    value,
                    qualification_registry=qualifications,
                )

    def test_malformed_rule_inside_accepted_q_fails_closed(self):
        claims = all_claims()
        key = next(iter(rule_claim("EXECUTIONS")))
        claims[key] = "caller says complete"
        with TemporaryDirectory() as directory:
            qualifications, _record, reconciliation = self.authorities(
                directory,
                claims=claims,
            )
            with self.assertRaisesRegex(
                ProviderAccountAbsenceSemanticsError,
                "EXECUTIONS.*malformed",
            ):
                self.resolve(qualifications, reconciliation)

    def test_claim_key_cannot_relabel_another_surface_payload(self):
        claims = all_claims()
        execution_key = next(iter(rule_claim("EXECUTIONS")))
        activity_raw = next(iter(rule_claim("ACTIVITIES").values()))
        claims[execution_key] = activity_raw
        with TemporaryDirectory() as directory:
            qualifications, _record, reconciliation = self.authorities(
                directory,
                claims=claims,
            )
            with self.assertRaisesRegex(
                ProviderAccountAbsenceSemanticsError,
                "key/surface mismatch",
            ):
                self.resolve(qualifications, reconciliation)

    def test_lookup_cannot_relabel_endpoint_or_entitlement(self):
        with TemporaryDirectory() as directory:
            qualifications, _record, reconciliation = self.authorities(directory)
            value = self.resolve(qualifications, reconciliation)
            for endpoint, entitlement in (
                ("/v5/order/history", "EXECUTIONS"),
                ("/v5/execution/list", "ORDERS"),
            ):
                with self.subTest(endpoint=endpoint, entitlement=entitlement):
                    with self.assertRaisesRegex(
                        ProviderAccountAbsenceSemanticsError,
                        "endpoint/entitlement mismatch",
                    ):
                        require_provider_account_absence_rule(
                            value,
                            surface="EXECUTIONS",
                            endpoint=endpoint,
                            data_entitlement=entitlement,
                            qualification_registry=qualifications,
                            at=NOW,
                        )

    def test_claim_helper_rejects_absolute_or_noncanonical_endpoint(self):
        for endpoint in (
            "https://api.bybit.com/v5/execution/list",
            "//v5/execution/list",
            "v5/execution/list",
            "/v5/execution/list?category=spot",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(
                    ProviderAccountAbsenceSemanticsError,
                    "provider-relative path",
                ):
                    account_reconciliation_absence_route_semantic(
                        surface="EXECUTIONS",
                        endpoint=endpoint,
                        data_entitlement="EXECUTIONS",
                        query_scope_rule_id="BYBIT_SPOT_ACCOUNT_QUERY_V1",
                        pagination_rule_id="BYBIT_V5_CURSOR_V1",
                        retention_rule_id="BYBIT_EXECUTION_RETENTION_V1",
                        consistency_horizon_rule_id="BYBIT_ACCOUNT_CONSISTENCY_HORIZON_V1",
                        semantics_version=1,
                    )

    def test_claim_helper_rejects_endpoint_path_aliases(self):
        for endpoint in (
            "/v5//execution/list",
            "/v5/./execution/list",
            "/v5/execution/../order/history",
            "/v5/execution/list/",
            "/v5/execution/%2e%2e/order/history",
            "/v5/%2Fexecution/list",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(
                    ProviderAccountAbsenceSemanticsError,
                    "provider-relative path",
                ):
                    account_reconciliation_absence_route_semantic(
                        surface="EXECUTIONS",
                        endpoint=endpoint,
                        data_entitlement="EXECUTIONS",
                        query_scope_rule_id="BYBIT_SPOT_ACCOUNT_QUERY_V1",
                        pagination_rule_id="BYBIT_V5_CURSOR_V1",
                        retention_rule_id="BYBIT_EXECUTION_RETENTION_V1",
                        consistency_horizon_rule_id="BYBIT_ACCOUNT_CONSISTENCY_HORIZON_V1",
                        semantics_version=1,
                    )

    def test_claim_helper_rejects_caller_free_form_rule_identity_and_bool_version(self):
        with self.assertRaisesRegex(
            ProviderAccountAbsenceSemanticsError,
            "query_scope_rule_id",
        ):
            account_reconciliation_absence_route_semantic(
                surface="EXECUTIONS",
                endpoint="/v5/execution/list",
                data_entitlement="EXECUTIONS",
                query_scope_rule_id="caller says account scoped",
                pagination_rule_id="BYBIT_V5_CURSOR_V1",
                retention_rule_id="BYBIT_EXECUTION_RETENTION_V1",
                consistency_horizon_rule_id="BYBIT_ACCOUNT_CONSISTENCY_HORIZON_V1",
                semantics_version=1,
            )
        with self.assertRaisesRegex(
            ProviderAccountAbsenceSemanticsError,
            "semantics_version",
        ):
            account_reconciliation_absence_route_semantic(
                surface="EXECUTIONS",
                endpoint="/v5/execution/list",
                data_entitlement="EXECUTIONS",
                query_scope_rule_id="BYBIT_SPOT_ACCOUNT_QUERY_V1",
                pagination_rule_id="BYBIT_V5_CURSOR_V1",
                retention_rule_id="BYBIT_EXECUTION_RETENTION_V1",
                consistency_horizon_rule_id="BYBIT_ACCOUNT_CONSISTENCY_HORIZON_V1",
                semantics_version=True,
            )

    def test_retention_rule_change_changes_accepted_semantics_identity(self):
        first_claims = all_claims()
        second_claims = all_claims()
        second_claims.update(
            rule_claim(
                "EXECUTIONS",
                retention_rule_id="BYBIT_EXECUTION_RETENTION_V2",
            )
        )
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            first_registry, first_record, first_reconciliation = self.authorities(
                first,
                claims=first_claims,
            )
            second_registry, second_record, second_reconciliation = self.authorities(
                second,
                claims=second_claims,
            )
            first_value = self.resolve(first_registry, first_reconciliation)
            second_value = self.resolve(second_registry, second_reconciliation)
            self.assertNotEqual(first_record.qualification_id, second_record.qualification_id)
            self.assertNotEqual(first_value.content_digest, second_value.content_digest)


if __name__ == "__main__":
    unittest.main()
