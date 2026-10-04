from dataclasses import replace
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.provider_route_financial_binding import (
    ProviderRouteFinancialBindingError,
    require_financial_binding_matches_selected_route,
)
from mvp.tests.test_financial_send_authority import binding as financial_binding
from mvp.tests.test_provider_route_dispatch import ProviderRouteDispatchTests


class ProviderRouteFinancialBindingTests(unittest.TestCase):
    def _route(self, directory: str):
        fixture = ProviderRouteDispatchTests(
            methodName="test_success_binds_q_and_c_into_durable_submission_scope"
        )
        self.addCleanup(fixture.doCleanups)
        _journal, _capabilities, _qualifications, route, _dispatcher, _q1, _harness = (
            fixture.setup_route(directory)
        )
        return route

    @staticmethod
    def _matching_binding(route):
        provider_scope = route.qualification.scope.provider_scope
        return replace(
            financial_binding(),
            provider_scope_digest=provider_scope.content_digest,
            provider_id=route.candidate.provider_id,
            account_id=route.candidate.account_id,
            runtime_environment=provider_scope.runtime_environment,
            provider_environment=route.candidate.provider_environment,
            entity_policy_id=route.candidate.entity_policy_id,
            capability_snapshot_id=route.capability_snapshot_id,
            qualification_identity_digest=route.qualification_id,
        )

    def test_exact_selected_route_c_and_q_match_financial_binding(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            require_financial_binding_matches_selected_route(
                self._matching_binding(route),
                route,
            )

    def test_other_qualification_id_cannot_relabel_financial_binding(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            binding = replace(
                self._matching_binding(route),
                qualification_identity_digest=(
                    "provider-qualification:sha256:" + "a" * 64
                ),
            )
            with self.assertRaisesRegex(
                ProviderRouteFinancialBindingError,
                "selected provider C/Q authority",
            ):
                require_financial_binding_matches_selected_route(binding, route)

    def test_other_capability_id_cannot_relabel_financial_binding(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            binding = replace(
                self._matching_binding(route),
                capability_snapshot_id="caller-selected-capability",
            )
            with self.assertRaisesRegex(
                ProviderRouteFinancialBindingError,
                "selected provider C/Q authority",
            ):
                require_financial_binding_matches_selected_route(binding, route)

    def test_provider_scope_digest_must_be_exact_selected_q_scope(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            binding = replace(
                self._matching_binding(route),
                provider_scope_digest=(
                    "provider-financial-scope:sha256:" + "b" * 64
                ),
            )
            with self.assertRaisesRegex(
                ProviderRouteFinancialBindingError,
                "selected provider C/Q authority",
            ):
                require_financial_binding_matches_selected_route(binding, route)

    def test_post_selection_static_route_retarget_is_rejected(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            binding = self._matching_binding(route)
            object.__setattr__(route.candidate, "account_id", "retargeted-account")
            with self.assertRaisesRegex(
                ProviderRouteFinancialBindingError,
                "accepted qualification|capability authority",
            ):
                require_financial_binding_matches_selected_route(binding, route)

    def test_selected_q_id_must_remain_its_content_identity(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            binding = self._matching_binding(route)
            object.__setattr__(
                route.qualification,
                "qualification_id",
                "provider-qualification:sha256:" + "c" * 64,
            )
            with self.assertRaisesRegex(
                ProviderRouteFinancialBindingError,
                "internally inconsistent",
            ):
                require_financial_binding_matches_selected_route(binding, route)


if __name__ == "__main__":
    unittest.main()
