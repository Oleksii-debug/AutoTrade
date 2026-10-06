from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.financial_send_authority import (
    FinancialSendAuthorityError,
    build_financial_send_authority_issuer,
)
from mvp.tests.test_provider_route_authority_composition import (
    SelectedRouteAuthorityCompositionTests,
)
from mvp.tests.test_provider_route_dispatch import ProviderRouteDispatchTests


class SelectedProviderRouteSealTests(unittest.TestCase):
    def _fixture(self, directory: str):
        fixture = ProviderRouteDispatchTests(
            methodName="test_success_binds_q_and_c_into_durable_submission_scope"
        )
        self.addCleanup(fixture.doCleanups)
        return fixture.setup_route(directory)

    def test_issuer_rejects_selected_route_decision_cut_mutation_after_composition(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            runtime = SelectedRouteAuthorityCompositionTests._runtime(directory, journal)
            issuer = build_financial_send_authority_issuer(
                AuthorityService(journal),
                runtime,
                selected_route=route,
                capability_registry=capabilities,
                qualification_registry=qualifications,
            )
            original_cut = route.decision_journal_sequence_cut

            # frozen=True prevents ordinary assignment, but it is not an
            # authority seal. Product composition must retain the exact selected
            # route cut outside caller-mutable object state and reject drift.
            object.__setattr__(
                route,
                "decision_journal_sequence_cut",
                original_cut + 1,
            )
            try:
                with self.assertRaisesRegex(
                    FinancialSendAuthorityError,
                    "selected provider route|route authority|changed",
                ):
                    _ = issuer.provider_route_bound
            finally:
                object.__setattr__(
                    route,
                    "decision_journal_sequence_cut",
                    original_cut,
                )


if __name__ == "__main__":
    unittest.main()
