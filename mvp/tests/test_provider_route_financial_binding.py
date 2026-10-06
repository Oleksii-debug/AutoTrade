from dataclasses import replace
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import payload_digest
from mvp.autotrade_mvp.provider_route_financial_binding import (
    ProviderRouteFinancialBindingError,
    build_selected_bybit_transport_authority_inputs,
    build_selected_provider_route_financial_submission_scope,
    build_selected_provider_route_transport_capability_registry,
    require_financial_binding_matches_selected_route,
)
from mvp.tests.test_financial_send_authority import binding as financial_binding
from mvp.tests.test_provider_route_dispatch import ProviderRouteDispatchTests
from mvp.tests.test_provider_selection import NOW


def _prepared_scope_kwargs(route):
    base = financial_binding()
    return {
        "endpoint": base.endpoint,
        "prepared_request_sha256": base.request_sha256,
        "capability_snapshot_ids": (route.capability_snapshot_id,),
        "instrument_versions": (str(base.instrument_version),),
    }


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
        base = financial_binding()
        submission_scope = build_selected_provider_route_financial_submission_scope(
            route,
            account_id=route.candidate.account_id,
            runtime_environment=provider_scope.runtime_environment,
            **_prepared_scope_kwargs(route),
        )
        return replace(
            base,
            provider_scope_digest=provider_scope.content_digest,
            provider_id=route.candidate.provider_id,
            account_id=route.candidate.account_id,
            runtime_environment=provider_scope.runtime_environment,
            provider_environment=route.candidate.provider_environment,
            entity_policy_id=route.candidate.entity_policy_id,
            capability_snapshot_id=route.capability_snapshot_id,
            qualification_identity_digest=route.qualification_id,
            submission_scope_digest=payload_digest(submission_scope),
        )

    def test_exact_selected_route_c_and_q_match_financial_binding(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            require_financial_binding_matches_selected_route(
                self._matching_binding(route),
                route,
            )

    def test_canonical_financial_submission_scope_is_constructible_from_selected_route(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            provider_scope = route.qualification.scope.provider_scope
            scope = build_selected_provider_route_financial_submission_scope(
                route,
                account_id=route.candidate.account_id,
                runtime_environment=provider_scope.runtime_environment,
                **_prepared_scope_kwargs(route),
            )
            binding = self._matching_binding(route)
            self.assertEqual(payload_digest(scope), binding.submission_scope_digest)
            self.assertEqual(scope["provider_id"], route.candidate.provider_id)
            self.assertEqual(scope["account_id"], route.candidate.account_id)
            self.assertEqual(
                scope["provider_environment"],
                route.candidate.provider_environment,
            )
            self.assertEqual(
                scope["provider_route_capability_snapshot_id"],
                route.capability_snapshot_id,
            )
            self.assertEqual(
                scope["provider_route_qualification_id"],
                route.qualification_id,
            )
            base = financial_binding()
            self.assertEqual(scope["endpoint"], base.endpoint)
            self.assertEqual(
                scope["prepared_request_sha256"],
                base.request_sha256,
            )
            self.assertEqual(
                scope["capability_snapshot_ids"],
                [route.capability_snapshot_id],
            )
            self.assertEqual(
                scope["instrument_versions"],
                [str(base.instrument_version)],
            )

    def test_canonical_financial_submission_scope_rejects_other_account(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            provider_scope = route.qualification.scope.provider_scope
            with self.assertRaisesRegex(
                ProviderRouteFinancialBindingError,
                "account differs from selected provider route",
            ):
                build_selected_provider_route_financial_submission_scope(
                    route,
                    account_id="other-account",
                    runtime_environment=provider_scope.runtime_environment,
                    **_prepared_scope_kwargs(route),
                )

    def test_canonical_financial_submission_scope_rejects_other_runtime(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            with self.assertRaisesRegex(
                ProviderRouteFinancialBindingError,
                "environment differs from selected provider route",
            ):
                build_selected_provider_route_financial_submission_scope(
                    route,
                    account_id=route.candidate.account_id,
                    runtime_environment="LIVE",
                    **_prepared_scope_kwargs(route),
                )

    def test_selected_route_c_is_directly_reusable_by_existing_transport_registry(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            registry = build_selected_provider_route_transport_capability_registry(route)
            current = registry.require_verified(
                provider_id=route.candidate.provider_id,
                account_id=route.candidate.account_id,
                entity_id=route.candidate.entity_id,
                environment=route.qualification.scope.provider_scope.runtime_environment,
                provider_environment=route.candidate.provider_environment,
                instrument_version=route.capability.instrument_version,
                at=NOW,
            )
            self.assertIs(current, route.capability)
            self.assertEqual(current.snapshot_id, route.capability_snapshot_id)

    def test_transport_registry_projection_rejects_retargeted_route_capability(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            object.__setattr__(route.capability, "account_id", "retargeted-account")
            with self.assertRaisesRegex(
                ProviderRouteFinancialBindingError,
                "capability differs from transport scope",
            ):
                build_selected_provider_route_transport_capability_registry(route)

    def test_bybit_transport_authority_inputs_all_come_from_selected_route(self):
        with TemporaryDirectory() as directory:
            route = self._route(directory)
            inputs = build_selected_bybit_transport_authority_inputs(route)
            self.assertEqual(
                set(inputs),
                {
                    "provider_environment",
                    "capability_snapshot_id",
                    "capability_registry",
                },
            )
            self.assertEqual(
                inputs["provider_environment"],
                route.candidate.provider_environment,
            )
            self.assertEqual(
                inputs["capability_snapshot_id"],
                route.capability_snapshot_id,
            )
            current = inputs["capability_registry"].require_verified(
                provider_id="BYBIT",
                account_id=route.candidate.account_id,
                entity_id=route.candidate.entity_id,
                environment=route.qualification.scope.provider_scope.runtime_environment,
                provider_environment=route.candidate.provider_environment,
                instrument_version=route.capability.instrument_version,
                at=NOW,
            )
            self.assertIs(current, route.capability)

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
