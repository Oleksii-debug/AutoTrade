import inspect
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.provider_account_empty_exclusion import (
    ProviderAccountEmptyExclusionError,
    ProviderAccountEmptyResultExclusionProof,
    issue_provider_account_empty_result_exclusion,
    require_current_provider_account_empty_exclusion_authority,
    require_provider_account_empty_exclusion_authority,
)
from mvp.tests.test_provider_account_coverage_set import ProviderAccountCoverageSetTests
from mvp.tests.test_provider_selection import NOW


class ProviderAccountEmptyExclusionTests(unittest.TestCase):
    def _fixture(self, directory: str):
        return ProviderAccountCoverageSetTests._fixture(self, directory)

    def _coverage_set(self, fixture):
        return ProviderAccountCoverageSetTests._issue(self, fixture)

    def _issue(self, fixture):
        coverage_set = self._coverage_set(fixture)
        return issue_provider_account_empty_result_exclusion(
            coverage_set=coverage_set,
            page_chains=tuple(
                fixture[6][surface] for surface in sorted(fixture[6])
            ),
            historical_submission=fixture[5],
            at=NOW,
        )

    def test_constructor_is_sealed(self):
        with self.assertRaisesRegex(
            ProviderAccountEmptyExclusionError,
            "canonical issuer",
        ):
            ProviderAccountEmptyResultExclusionProof()

    def test_four_empty_provider_result_sets_issue_exclusion_proof(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            value = self._issue(fixture)
            self.assertEqual(
                tuple(item["surface"] for item in value.surfaces),
                ("ACTIVITIES", "EXECUTIONS", "OPEN_ORDERS", "ORDER_HISTORY"),
            )
            self.assertTrue(all(item["total_rows"] == 0 for item in value.surfaces))
            self.assertEqual(value.client_order_id, fixture[5].client_order_id)
            self.assertEqual(
                value.exclusion_rule_id,
                "STRICT_EMPTY_PROVIDER_RESULT_SET_V1",
            )
            self.assertTrue(
                value.content_digest.startswith(
                    "provider-account-empty-exclusion:sha256:"
                )
            )
            require_provider_account_empty_exclusion_authority(value)
            self.assertIs(
                require_current_provider_account_empty_exclusion_authority(
                    value,
                    at=NOW,
                ),
                value,
            )

    def test_missing_or_duplicate_page_chain_fails_closed(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            coverage_set = self._coverage_set(fixture)
            ordered = tuple(
                fixture[6][surface] for surface in sorted(fixture[6])
            )
            with self.assertRaisesRegex(
                ProviderAccountEmptyExclusionError,
                "exactly four",
            ):
                issue_provider_account_empty_result_exclusion(
                    coverage_set=coverage_set,
                    page_chains=ordered[:-1],
                    historical_submission=fixture[5],
                    at=NOW,
                )
            duplicated = ordered[:-1] + (ordered[0],)
            with self.assertRaisesRegex(
                ProviderAccountEmptyExclusionError,
                "duplicate",
            ):
                issue_provider_account_empty_result_exclusion(
                    coverage_set=coverage_set,
                    page_chains=duplicated,
                    historical_submission=fixture[5],
                    at=NOW,
                )

    def test_non_authoritative_page_chain_value_is_rejected_by_exact_type(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            coverage_set = self._coverage_set(fixture)
            page_chains = tuple(
                fixture[6][surface] for surface in sorted(fixture[6])
            )
            with self.assertRaisesRegex(
                TypeError,
                "page_chains must be an exact tuple of ProviderAccountPageChain",
            ):
                issue_provider_account_empty_result_exclusion(
                    coverage_set=coverage_set,
                    page_chains=page_chains[:-1] + (object(),),
                    historical_submission=fixture[5],
                    at=NOW,
                )

    def test_new_acquisition_revokes_current_exclusion_but_keeps_history(self):
        with TemporaryDirectory() as directory:
            fixture = self._fixture(directory)
            value = self._issue(fixture)
            acquisition_authority, acquisition = fixture[1:3]
            acquisition_authority.issue_serialized(
                provider_scope=acquisition.provider_scope,
                account_id=acquisition.account_id,
                acquisition_request_id="empty-exclusion-acquisition-2",
                committed_at=NOW,
            )
            require_provider_account_empty_exclusion_authority(value)
            with self.assertRaises(ProviderAccountEmptyExclusionError):
                require_current_provider_account_empty_exclusion_authority(
                    value,
                    at=NOW,
                )

    def test_issuer_has_no_caller_absence_or_horizon_verdict(self):
        parameters = inspect.signature(
            issue_provider_account_empty_result_exclusion
        ).parameters
        for forbidden in (
            "provider_semantics_exclude_execution",
            "consistency_horizon_satisfied",
            "horizon_elapsed",
            "pagination_complete",
            "proven_absent",
            "coverage_complete",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)


if __name__ == "__main__":
    unittest.main()
