from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import durable_provider_qualification as durable_q_module
from mvp.autotrade_mvp import provider_account_reconciliation_semantics as semantics_module
from mvp.autotrade_mvp.durable_provider_qualification import (
    DurableProviderQualificationRegistry,
)
from mvp.autotrade_mvp.persistence import JournalStore
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

    def test_public_registry_method_rebinding_does_not_redirect_resolution(self):
        touched = []

        def bomb(*_args, **_kwargs):
            touched.append("registry")
            raise AssertionError("rebound provider-Q registry method executed")

        with TemporaryDirectory() as directory:
            registry, record = self._authorities(
                directory,
                claims=self._claims(),
            )
            with (
                patch.object(
                    DurableProviderQualificationRegistry,
                    "qualification",
                    bomb,
                ),
                patch.object(
                    DurableProviderQualificationRegistry,
                    "require_exact_current",
                    bomb,
                ),
            ):
                value = resolve_current_provider_account_reconciliation_semantics(
                    qualification_registry=registry,
                    qualification_id=record.qualification_id,
                    provider_scope_digest=record.scope.provider_scope.content_digest,
                    at=NOW,
                )

            self.assertEqual(touched, [])
            self.assertEqual(
                value.qualification_id,
                record.qualification_id,
            )

    def test_public_module_alias_and_helper_rebinding_do_not_redirect_resolution(self):
        touched = []

        class ForeignRegistry:
            pass

        class ForeignCurrentScope:
            def __init__(self, *_args, **_kwargs):
                touched.append("scope")
                raise AssertionError("rebound current-scope class executed")

        class ForeignSemantics:
            pass

        def bomb(*_args, **_kwargs):
            touched.append("helper")
            raise AssertionError("rebound canonical helper executed")

        class ForeignJson:
            loads = staticmethod(bomb)

        with TemporaryDirectory() as directory:
            registry, record = self._authorities(
                directory,
                claims=self._claims(),
            )
            with (
                patch.object(
                    semantics_module,
                    "DurableProviderQualificationRegistry",
                    ForeignRegistry,
                ),
                patch.object(
                    semantics_module,
                    "ProviderQualificationCurrentScope",
                    ForeignCurrentScope,
                ),
                patch.object(
                    semantics_module,
                    "QualifiedProviderAccountReconciliationSemantics",
                    ForeignSemantics,
                ),
                patch.object(
                    semantics_module,
                    "require_provider_account_reconciliation_semantics_authority",
                    bomb,
                ),
                patch.object(semantics_module, "canonical_json", bomb),
                patch.object(semantics_module, "sha256", bomb),
                patch.object(semantics_module, "json", ForeignJson),
            ):
                value = resolve_current_provider_account_reconciliation_semantics(
                    qualification_registry=registry,
                    qualification_id=record.qualification_id,
                    provider_scope_digest=record.scope.provider_scope.content_digest,
                    at=NOW,
                )
                digest = value.content_digest

            self.assertEqual(touched, [])
            self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(
                value.qualification_id,
                record.qualification_id,
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

    def test_registry_store_retarget_during_authenticated_q_replay_fails_closed(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as other:
            registry, record = self._authorities(
                directory,
                claims=self._claims(),
            )
            foreign_store = JournalStore(Path(other) / "foreign.sqlite3")
            original_verify = durable_q_module.verify_provider_qualification_campaign
            mutation_count = 0

            def verify_then_retarget(*args, **kwargs):
                nonlocal mutation_count
                result = original_verify(*args, **kwargs)
                mutation_count += 1
                registry.store = foreign_store
                return result

            with (
                patch.object(
                    durable_q_module,
                    "verify_provider_qualification_campaign",
                    side_effect=verify_then_retarget,
                ),
                self.assertRaisesRegex(
                    ProviderAccountReconciliationSemanticsError,
                    "registry store changed during resolution",
                ),
            ):
                resolve_current_provider_account_reconciliation_semantics(
                    qualification_registry=registry,
                    qualification_id=record.qualification_id,
                    provider_scope_digest=record.scope.provider_scope.content_digest,
                    at=NOW,
                )

            self.assertGreaterEqual(mutation_count, 1)
            self.assertEqual(foreign_store.current_journal_sequence(), 0)

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
