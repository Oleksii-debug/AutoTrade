import inspect
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.provider_account_accepted_cut import (
    AcceptedProviderAccountCut,
    AcceptedProviderAccountCutError,
    issue_accepted_serialized_readback_account_cut,
    require_accepted_provider_account_cut_authority,
    require_current_accepted_provider_account_cut_authority,
)
from mvp.tests.test_provider_account_coverage_set import ProviderAccountCoverageSetTests
from mvp.tests.test_provider_selection import NOW


class AcceptedProviderAccountCutTests(unittest.TestCase):
    def _fixture(self, directory: str):
        return ProviderAccountCoverageSetTests._fixture(self, directory)

    def _coverage_set(self, fixture):
        return ProviderAccountCoverageSetTests._issue(self, fixture)

    def _issue(self, fixture):
        return issue_accepted_serialized_readback_account_cut(
            account_acquisition_authority=fixture[1],
            account_acquisition=fixture[2],
            origin_set=fixture[3],
            coverage_set=self._coverage_set(fixture),
            qualification_registry=fixture[0],
            at=NOW,
        )

    def test_constructor_is_sealed(self):
        with self.assertRaisesRegex(
            AcceptedProviderAccountCutError,
            "canonical issuer",
        ):
            AcceptedProviderAccountCut()

    def test_exact_current_sources_issue_serialized_readback_cut(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            coverage_set = self._coverage_set(fixture)
            value = issue_accepted_serialized_readback_account_cut(
                account_acquisition_authority=fixture[1],
                account_acquisition=fixture[2],
                origin_set=fixture[3],
                coverage_set=coverage_set,
                qualification_registry=fixture[0],
                at=NOW,
            )
            identity = value.identity
            self.assertEqual(
                identity.acquisition_mode,
                "SERIALIZED_ACQUISITION_GENERATION",
            )
            self.assertEqual(
                identity.consistency_method_id,
                "SERIALIZED_SNAPSHOT_READBACK",
            )
            self.assertEqual(identity.consistency_method_version, 1)
            self.assertIsNone(identity.stream_binding_set_digest)
            self.assertIsNone(identity.backfill_binding_set_digest)
            self.assertIsNone(identity.provider_native_generation_token)
            self.assertEqual(identity.account_id, fixture[2].account_id)
            self.assertEqual(identity.acquisition_id, fixture[2].acquisition_id)
            self.assertEqual(
                identity.acquisition_generation,
                fixture[2].acquisition_generation,
            )
            self.assertEqual(
                identity.acquisition_journal_sequence_cut,
                fixture[2].acquisition_journal_sequence_cut,
            )
            self.assertEqual(
                identity.qualification_identity_digest,
                fixture[3].qualification_id,
            )
            self.assertEqual(
                identity.origin_binding_set_digest,
                fixture[3].content_digest,
            )
            self.assertEqual(
                value.required_coverage_set_digest,
                coverage_set.content_digest,
            )
            self.assertTrue(
                identity.content_digest.startswith("provider-account-cut:sha256:")
            )
            self.assertTrue(
                value.content_digest.startswith("accepted-provider-account-cut:sha256:")
            )
            require_accepted_provider_account_cut_authority(value)
            self.assertIs(
                require_current_accepted_provider_account_cut_authority(
                    value,
                    at=NOW,
                ),
                value,
            )

    def test_new_acquisition_revokes_current_cut_but_historical_cut_remains(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            value = self._issue(fixture)
            acquisition_authority = fixture[1]
            acquisition = fixture[2]
            acquisition_authority.issue_serialized(
                provider_scope=acquisition.provider_scope,
                account_id=acquisition.account_id,
                acquisition_request_id="accepted-cut-acquisition-2",
                committed_at=NOW,
            )
            self.assertIs(
                require_accepted_provider_account_cut_authority(value),
                value,
            )
            with self.assertRaisesRegex(
                AcceptedProviderAccountCutError,
                "not exact current authority",
            ):
                require_current_accepted_provider_account_cut_authority(
                    value,
                    at=NOW,
                )

    def test_non_authoritative_coverage_value_is_rejected_by_exact_type(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            with self.assertRaisesRegex(
                TypeError,
                "coverage_set must be exact ProviderAccountRequiredSurfaceCoverageSet",
            ):
                issue_accepted_serialized_readback_account_cut(
                    account_acquisition_authority=fixture[1],
                    account_acquisition=fixture[2],
                    origin_set=fixture[3],
                    coverage_set=object(),
                    qualification_registry=fixture[0],
                    at=NOW,
                )

    def test_issuer_has_no_caller_consistency_or_terminal_verdict_inputs(self):
        parameters = inspect.signature(
            issue_accepted_serialized_readback_account_cut
        ).parameters
        for forbidden in (
            "mode",
            "acquisition_mode",
            "snapshot_consistent",
            "consistency_method_id",
            "consistency_method_version",
            "pagination_complete",
            "coverage_complete",
            "consistency_horizon_satisfied",
            "horizon_elapsed",
            "proven_absent",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)


if __name__ == "__main__":
    unittest.main()
