from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import provider_route_dispatch
from mvp.tests.test_provider_route_dispatch import ProviderRouteDispatchTests
from mvp.tests.test_provider_selection import NOW


_AT = NOW.isoformat().replace("+00:00", "Z")


class ProviderRouteTransitiveAuthorityTests(unittest.TestCase):
    def _fixture(self, directory: str):
        fixture = ProviderRouteDispatchTests(
            methodName="test_success_binds_q_and_c_into_durable_submission_scope"
        )
        self.addCleanup(fixture.doCleanups)
        return fixture.setup_route(directory)

    def test_composed_c_q_barrier_cannot_retarget_journal_cut_helper_after_composition(self):
        """Pin transitive barrier executables, not only the outer composer."""

        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            combined = provider_route_dispatch.compose_selected_provider_route_authority(
                store=journal,
                environment="PAPER",
                account_id="paper-account",
                route=route,
                capability_registry=capabilities,
                qualification_registry=qualifications,
                authority_check=lambda _intent_hash, _at: (
                    True,
                    "financial_authority_current",
                ),
            )

            original = provider_route_dispatch._journal_cut
            calls = []

            def forged(_store):
                calls.append("forged")
                raise AssertionError("forged journal-cut authority executed")

            provider_route_dispatch._journal_cut = forged
            try:
                # A provider-route authority composed under the original code
                # must fail closed on executable retargeting before calling the
                # replacement. Merely pinning the outer composer is insufficient
                # because the returned closure otherwise resolves globals later.
                result = combined("intent-hash", _AT)
                self.assertEqual(
                    result,
                    (False, "provider_route_executable_authority_changed"),
                )
            finally:
                provider_route_dispatch._journal_cut = original

            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
