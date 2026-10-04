from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.provider_account_reconciliation_semantics import (
    ProviderAccountReconciliationSemanticsError,
    QualifiedProviderAccountReconciliationSemantics,
    account_reconciliation_route_semantics,
    require_provider_account_reconciliation_semantics_authority,
    resolve_current_provider_account_reconciliation_semantics,
)
from mvp.tests.test_provider_selection import ProviderSelectionTests, NOW


class ProviderAccountReconciliationSemanticsTests(unittest.TestCase):
    def _authorities(self, directory: str, *, claims=None):
        helper = ProviderSelectionTests(
            methodName="test_exact_current_c_and_q_are_selected_at_one_journal_cut"
        )
        self.addCleanup(helper.doCleanups)
        _capabilities, qualifications, record = helper.authorities(
            directory,
            extra_route_semantics=claims,
        )
        return qualifications, record

    def _claims(self):
        return account_reconciliation_route_semantics(
            acquisition_mode="SERIALIZED_ACQUISITION_GENERATION",
            consistency_method_id="SERIALIZED_SNAPSHOT_READBACK",
            consistency_method_version=1,
        )

    def test_missing_source_owned_q_semantics_fail_closed(self):
        with TemporaryDirectory() as directory:
            registry, record = self._authorities(directory)
            with self.assertRaisesRegex(
                ProviderAccountReconciliationSemanticsError,
                "lacks source-owned account reconciliation semantics",
            ):
                resolve_current_provider_account_reconciliation_semantics(
                    qualification_registry=registry,
                    qualification_id=record.qualification_id,
                    provider_scope_digest=record.scope.provider_scope.content_digest,
                    at=NOW,
                )

    def test_exact_current_q_issues_sealed_reconciliation_semantics(self):
        with TemporaryDirectory() as directory:
            registry, record = self._authorities(
                directory,
                claims=self._claims(),
            )
            value = resolve_current_provider_account_reconciliation_semantics(
                qualification_registry=registry,
                qualification_id=record.qualification_id,
                provider_scope_digest=record.scope.provider_scope.content_digest,
                at=NOW,
            )
            self.assertEqual(
                value.acquisition_mode,
                "SERIALIZED_ACQUISITION_GENERATION",
            )
            self.assertEqual(
                value.consistency_method_id,
                "SERIALIZED_SNAPSHOT_READBACK",
            )
            self.assertEqual(value.consistency_method_version, 1)
            self.assertEqual(
                value.qualification_route_semantics_digest,
                record.identity.route_semantics_digest,
            )
            self.assertTrue(value.content_digest.startswith("sha256:"))
            require_provider_account_reconciliation_semantics_authority(
                value,
                qualification_registry=registry,
            )

    def test_constructor_cannot_forge_q_semantics(self):
        with self.assertRaisesRegex(
            ProviderAccountReconciliationSemanticsError,
            "must come from exact current provider Q",
        ):
            QualifiedProviderAccountReconciliationSemantics()

    def test_post_issue_mutation_is_detected(self):
        with TemporaryDirectory() as directory:
            registry, record = self._authorities(
                directory,
                claims=self._claims(),
            )
            value = resolve_current_provider_account_reconciliation_semantics(
                qualification_registry=registry,
                qualification_id=record.qualification_id,
                provider_scope_digest=record.scope.provider_scope.content_digest,
                at=NOW,
            )
            object.__setattr__(
                value,
                "consistency_method_id",
                "FORGED_METHOD",
            )
            with self.assertRaisesRegex(
                ProviderAccountReconciliationSemanticsError,
                "changed after Q resolution",
            ):
                require_provider_account_reconciliation_semantics_authority(
                    value,
                    qualification_registry=registry,
                )

    def test_wrong_provider_scope_digest_is_rejected(self):
        with TemporaryDirectory() as directory:
            registry, record = self._authorities(
                directory,
                claims=self._claims(),
            )
            wrong = "provider-financial-scope:sha256:" + "0" * 64
            self.assertNotEqual(
                wrong,
                record.scope.provider_scope.content_digest,
            )
            with self.assertRaisesRegex(
                ProviderAccountReconciliationSemanticsError,
                "Q scope mismatch",
            ):
                resolve_current_provider_account_reconciliation_semantics(
                    qualification_registry=registry,
                    qualification_id=record.qualification_id,
                    provider_scope_digest=wrong,
                    at=NOW,
                )

    def test_expired_q_cannot_issue_reconciliation_semantics(self):
        with TemporaryDirectory() as directory:
            registry, record = self._authorities(
                directory,
                claims=self._claims(),
            )
            with self.assertRaisesRegex(
                ProviderAccountReconciliationSemanticsError,
                "not exact current authority",
            ):
                resolve_current_provider_account_reconciliation_semantics(
                    qualification_registry=registry,
                    qualification_id=record.qualification_id,
                    provider_scope_digest=record.scope.provider_scope.content_digest,
                    at=datetime(2026, 10, 6, tzinfo=timezone.utc),
                )

    def test_noncanonical_method_claim_is_rejected_even_when_inside_q(self):
        claims = self._claims()
        claims["ACCOUNT_RECONCILIATION_CONSISTENCY_METHOD_ID"] = "caller says consistent"
        with TemporaryDirectory() as directory:
            registry, record = self._authorities(
                directory,
                claims=claims,
            )
            with self.assertRaisesRegex(
                ProviderAccountReconciliationSemanticsError,
                "consistency_method_id",
            ):
                resolve_current_provider_account_reconciliation_semantics(
                    qualification_registry=registry,
                    qualification_id=record.qualification_id,
                    provider_scope_digest=record.scope.provider_scope.content_digest,
                    at=NOW,
                )

    def test_cross_store_semantics_authority_is_rejected(self):
        claims = self._claims()
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            first_registry, first_record = self._authorities(
                first,
                claims=claims,
            )
            second_registry, second_record = self._authorities(
                second,
                claims=claims,
            )
            self.assertEqual(
                first_record.qualification_id,
                second_record.qualification_id,
            )
            value = resolve_current_provider_account_reconciliation_semantics(
                qualification_registry=first_registry,
                qualification_id=first_record.qualification_id,
                provider_scope_digest=
                    first_record.scope.provider_scope.content_digest,
                at=NOW,
            )
            with self.assertRaisesRegex(
                ProviderAccountReconciliationSemanticsError,
                "same exact JournalStore generation",
            ):
                require_provider_account_reconciliation_semantics_authority(
                    value,
                    qualification_registry=second_registry,
                )


if __name__ == "__main__":
    unittest.main()
