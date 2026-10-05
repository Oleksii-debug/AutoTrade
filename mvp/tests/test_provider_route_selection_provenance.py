from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import provider_selection
from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.financial_send_authority import (
    FinancialSendAuthorityError,
    build_financial_send_authority_issuer,
)
from mvp.autotrade_mvp.provider_selection import SelectedProviderRoute
from mvp.tests.test_provider_route_authority_composition import (
    SelectedRouteAuthorityCompositionTests,
)
from mvp.tests.test_provider_route_dispatch import ProviderRouteDispatchTests


class SelectedProviderRouteProvenanceTests(unittest.TestCase):
    def _fixture(self, directory: str):
        fixture = ProviderRouteDispatchTests(
            methodName="test_success_binds_q_and_c_into_durable_submission_scope"
        )
        self.addCleanup(fixture.doCleanups)
        return fixture.setup_route(directory)

    def test_importable_selection_token_cannot_self_mint_product_route_authority(self):
        """A private-name Python token is not canonical provider selection authority."""

        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, selected, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            token = getattr(provider_selection, "_SELECTED_ROUTE_TOKEN", None)
            self.assertIsNone(
                token,
                "canonical selection must not expose a caller-reusable mint token",
            )

            # Even if a caller has the exact current C/Q objects, it must not be
            # able to manufacture the value that product composition treats as
            # proof that canonical selection policy actually chose this route.
            with self.assertRaisesRegex(
                (TypeError, ValueError, PermissionError),
                "canonical provider selection|selection authority|selected route",
            ):
                SelectedProviderRoute(
                    candidate=selected.candidate,
                    capability=selected.capability,
                    qualification=selected.qualification,
                    decision_journal_sequence_cut=(
                        selected.decision_journal_sequence_cut
                    ),
                    _selection_token=token,
                )

    def test_financial_issuer_rejects_unissued_exact_route_object(self):
        """Exact type plus genuine C/Q values still do not prove canonical selection."""

        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, selected, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            forged = object.__new__(SelectedProviderRoute)
            object.__setattr__(forged, "candidate", selected.candidate)
            object.__setattr__(forged, "capability", selected.capability)
            object.__setattr__(forged, "qualification", selected.qualification)
            object.__setattr__(
                forged,
                "decision_journal_sequence_cut",
                selected.decision_journal_sequence_cut,
            )
            runtime = SelectedRouteAuthorityCompositionTests._runtime(directory, journal)

            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "selected provider route|route authority|changed",
            ):
                build_financial_send_authority_issuer(
                    AuthorityService(journal),
                    runtime,
                    selected_route=forged,
                    capability_registry=capabilities,
                    qualification_registry=qualifications,
                )


if __name__ == "__main__":
    unittest.main()
