from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import provider_route_dispatch
from mvp.autotrade_mvp.provider_route_dispatch import ProviderRouteDispatchError
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

    @staticmethod
    def _compose(journal, capabilities, qualifications, route):
        return provider_route_dispatch.compose_selected_provider_route_authority(
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

    @staticmethod
    def _assert_retarget_rejected_without_call(combined, calls):
        result = combined("intent-hash", _AT)
        if result != (False, "provider_route_executable_authority_changed"):
            raise AssertionError(
                "retargeted provider-route executable was not rejected canonically: "
                f"{result!r}"
            )
        if calls:
            raise AssertionError(f"retargeted executable was invoked: {calls!r}")

    def test_composer_rejects_bound_route_helper_retarget_before_composition(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            original = provider_route_dispatch._require_bound_route_authority
            calls = []

            def forged(**_kwargs):
                calls.append("forged")
                raise AssertionError("forged route-binding authority executed")

            provider_route_dispatch._require_bound_route_authority = forged
            try:
                with self.assertRaisesRegex(
                    ProviderRouteDispatchError,
                    "executable authority changed",
                ):
                    self._compose(journal, capabilities, qualifications, route)
            finally:
                provider_route_dispatch._require_bound_route_authority = original
            self.assertEqual(calls, [])

    def test_composer_rejects_current_scope_helper_retarget_before_composition(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            original = provider_route_dispatch._current_scope
            calls = []

            def forged(_route):
                calls.append("forged")
                raise AssertionError("forged current-scope authority executed")

            provider_route_dispatch._current_scope = forged
            try:
                with self.assertRaisesRegex(
                    ProviderRouteDispatchError,
                    "executable authority changed",
                ):
                    self._compose(journal, capabilities, qualifications, route)
            finally:
                provider_route_dispatch._current_scope = original
            self.assertEqual(calls, [])

    def test_composer_rejects_time_parser_retarget_before_composition(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            original = provider_route_dispatch._point
            calls = []

            def forged(_value):
                calls.append("forged")
                raise AssertionError("forged barrier time parser executed")

            provider_route_dispatch._point = forged
            try:
                with self.assertRaisesRegex(
                    ProviderRouteDispatchError,
                    "executable authority changed",
                ):
                    self._compose(journal, capabilities, qualifications, route)
            finally:
                provider_route_dispatch._point = original
            self.assertEqual(calls, [])

    def test_composed_c_q_barrier_cannot_retarget_time_parser_after_composition(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            combined = self._compose(journal, capabilities, qualifications, route)
            original = provider_route_dispatch._point
            calls = []

            def forged(_value):
                calls.append("forged")
                raise AssertionError("forged barrier time parser executed")

            provider_route_dispatch._point = forged
            try:
                self._assert_retarget_rejected_without_call(combined, calls)
            finally:
                provider_route_dispatch._point = original

    def test_composed_c_q_barrier_cannot_retarget_journal_cut_helper_after_composition(self):
        """Pin transitive barrier executables, not only the outer composer."""

        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            combined = self._compose(journal, capabilities, qualifications, route)

            original = provider_route_dispatch._journal_cut
            calls = []

            def forged(_store):
                calls.append("forged")
                raise AssertionError("forged journal-cut authority executed")

            provider_route_dispatch._journal_cut = forged
            try:
                self._assert_retarget_rejected_without_call(combined, calls)
            finally:
                provider_route_dispatch._journal_cut = original

    def test_composed_c_q_barrier_rejects_store_cut_instance_shadow_before_call(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            combined = self._compose(journal, capabilities, qualifications, route)
            calls = []

            def forged():
                calls.append("forged")
                raise AssertionError("forged whole-store cut executed")

            journal.whole_store_state_cut = forged
            try:
                self._assert_retarget_rejected_without_call(combined, calls)
            finally:
                del journal.whole_store_state_cut

    def test_composed_c_q_barrier_rejects_capability_reader_instance_shadow_before_call(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            combined = self._compose(journal, capabilities, qualifications, route)
            calls = []

            def forged(**_kwargs):
                calls.append("forged")
                raise AssertionError("forged capability reader executed")

            capabilities.require_verified = forged
            try:
                self._assert_retarget_rejected_without_call(combined, calls)
            finally:
                del capabilities.require_verified

    def test_composed_c_q_barrier_rejects_qualification_reader_instance_shadow_before_call(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            combined = self._compose(journal, capabilities, qualifications, route)
            calls = []

            def forged(**_kwargs):
                calls.append("forged")
                raise AssertionError("forged qualification reader executed")

            qualifications.require_exact_current = forged
            try:
                self._assert_retarget_rejected_without_call(combined, calls)
            finally:
                del qualifications.require_exact_current


if __name__ == "__main__":
    unittest.main()
