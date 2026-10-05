from datetime import timedelta
import inspect
import json
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import stable_client_order_id
from mvp.autotrade_mvp.provider_account_absence_coverage import (
    issue_provider_account_surface_coverage,
    resolve_historical_unknown_submission,
)
from mvp.autotrade_mvp.provider_account_page_chain import issue_provider_account_page_chain
from mvp.autotrade_mvp.provider_account_surface_facts import (
    ProviderAccountSurfaceFactScan,
    ProviderAccountSurfaceFactsError,
    issue_provider_account_surface_fact_scan,
    require_provider_account_surface_fact_scan_authority,
)
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_route_reads import prepare_qualified_provider_read
from mvp.tests.test_provider_account_absence_coverage import (
    ATTEMPT_ID,
    INTENT_ID,
    _append_unknown,
    _milliseconds,
)
from mvp.tests.test_provider_account_page_chain import (
    ENDPOINT,
    SURFACE,
    ProviderAccountPageChainTests,
)
from mvp.tests.test_provider_selection import NOW


def _body(rows: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "retCode": 0,
            "result": {
                "list": rows,
                "nextPageCursor": "",
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class ProviderAccountSurfaceFactScanTests(unittest.TestCase):
    def _fixture(self, directory: str, *, row_builder=None):
        base = ProviderAccountPageChainTests._fixture(self, directory)
        (
            journal,
            capabilities,
            qualifications,
            route,
            _origin,
            _acquisition_authority,
            _acquisition,
            absence,
        ) = base
        account_id = route.candidate.account_id
        environment = route.capability.environment
        client_order_id = stable_client_order_id(
            "BYBIT",
            INTENT_ID,
            environment=environment,
            account_id=account_id,
        )
        send_at = NOW - timedelta(minutes=10)
        _append_unknown(
            journal,
            environment=environment,
            account_id=account_id,
            provider="BYBIT",
            intent_id=INTENT_ID,
            attempt_id=ATTEMPT_ID,
            client_order_id=client_order_id,
            sent_at=send_at,
        )
        historical = resolve_historical_unknown_submission(
            journal,
            environment=environment,
            account_id=account_id,
            attempt_id=ATTEMPT_ID,
        )
        binding = prepare_qualified_provider_read(
            route,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=ENDPOINT,
            query={
                "category": "spot",
                "orderLinkId": client_order_id,
                "startTime": _milliseconds(send_at - timedelta(minutes=1)),
                "endTime": _milliseconds(NOW - timedelta(minutes=1)),
                "limit": "100",
            },
            at=NOW,
            permission_scope="ORDER.READ",
        )
        rows = [] if row_builder is None else row_builder(client_order_id)
        response = ProviderAccountPageChainTests._direct_response(
            self,
            base,
            binding,
            body=_body(rows),
            marker="surface-facts",
        )
        origin_set = ProviderAccountPageChainTests._origin_set(
            self,
            base,
            (response,),
        )
        observation = ProviderAccountPageChainTests._observation(
            self,
            response,
            binding,
        )
        page_chain = issue_provider_account_page_chain(
            absence_semantics=absence,
            origin_set=origin_set,
            observations=(observation,),
            qualification_registry=qualifications,
            surface=SURFACE,
            at=NOW,
        )
        coverage = issue_provider_account_surface_coverage(
            absence_semantics=absence,
            page_chain=page_chain,
            historical_submission=historical,
            qualification_registry=qualifications,
            at=NOW,
        )
        return coverage, page_chain, historical, observation, client_order_id

    def test_empty_exact_target_page_issues_no_match_row_fact(self):
        with TemporaryDirectory() as directory:
            coverage, page_chain, historical, observation, _client_id = self._fixture(
                directory
            )
            value = issue_provider_account_surface_fact_scan(
                coverage=coverage,
                page_chain=page_chain,
                historical_submission=historical,
                observations=(observation,),
            )
            self.assertIsInstance(value, ProviderAccountSurfaceFactScan)
            self.assertEqual(value.scan_state, "NO_MATCHING_ROW_OBSERVED")
            self.assertEqual(value.scanned_page_count, 1)
            self.assertEqual(value.scanned_row_count, 0)
            self.assertEqual(value.matching_row_count, 0)
            self.assertEqual(value.matching_row_refs, ())
            require_provider_account_surface_fact_scan_authority(value)

    def test_matching_exact_target_row_is_source_derived_fact(self):
        with TemporaryDirectory() as directory:
            coverage, page_chain, historical, observation, client_id = self._fixture(
                directory,
                row_builder=lambda target: [
                    {
                        "execId": "provider-execution-1",
                        "orderLinkId": target,
                    }
                ],
            )
            value = issue_provider_account_surface_fact_scan(
                coverage=coverage,
                page_chain=page_chain,
                historical_submission=historical,
                observations=(observation,),
            )
            self.assertEqual(value.target_client_order_id, client_id)
            self.assertEqual(value.scan_state, "MATCHING_ROW_OBSERVED")
            self.assertEqual(value.scanned_row_count, 1)
            self.assertEqual(value.matching_row_count, 1)
            self.assertEqual(len(value.matching_row_refs), 1)
            self.assertTrue(
                value.matching_row_refs[0].startswith(observation.origin_ref + "#row:")
            )

    def test_exact_target_query_cannot_hide_contradictory_provider_row(self):
        with TemporaryDirectory() as directory:
            coverage, page_chain, historical, observation, _client_id = self._fixture(
                directory,
                row_builder=lambda _target: [
                    {
                        "execId": "contradictory-provider-execution",
                        "orderLinkId": "different-client-order-id",
                    }
                ],
            )
            with self.assertRaisesRegex(
                ProviderAccountSurfaceFactsError,
                "contradictory orderLinkId",
            ):
                issue_provider_account_surface_fact_scan(
                    coverage=coverage,
                    page_chain=page_chain,
                    historical_submission=historical,
                    observations=(observation,),
                )

    def test_row_without_order_link_id_fails_closed(self):
        with TemporaryDirectory() as directory:
            coverage, page_chain, historical, observation, _client_id = self._fixture(
                directory,
                row_builder=lambda _target: [{"execId": "missing-link-id"}],
            )
            with self.assertRaisesRegex(
                ProviderAccountSurfaceFactsError,
                "lacks exact orderLinkId",
            ):
                issue_provider_account_surface_fact_scan(
                    coverage=coverage,
                    page_chain=page_chain,
                    historical_submission=historical,
                    observations=(observation,),
                )

    def test_issuer_accepts_no_caller_presence_or_absence_verdict(self):
        parameters = inspect.signature(
            issue_provider_account_surface_fact_scan
        ).parameters
        for forbidden in (
            "present",
            "absent",
            "matching_row_count",
            "searched_client_order_ids",
            "provider_semantics_exclude_execution",
            "consistency_horizon_satisfied",
            "pagination_complete",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)

    def test_observations_must_exactly_cover_issued_page_chain(self):
        with TemporaryDirectory() as directory:
            coverage, page_chain, historical, observation, _client_id = self._fixture(
                directory
            )
            with self.assertRaisesRegex(
                ProviderAccountSurfaceFactsError,
                "duplicate origin observation",
            ):
                issue_provider_account_surface_fact_scan(
                    coverage=coverage,
                    page_chain=page_chain,
                    historical_submission=historical,
                    observations=(observation, observation),
                )

    def test_post_issue_source_mutation_invalidates_scan_authority(self):
        with TemporaryDirectory() as directory:
            coverage, page_chain, historical, observation, _client_id = self._fixture(
                directory
            )
            value = issue_provider_account_surface_fact_scan(
                coverage=coverage,
                page_chain=page_chain,
                historical_submission=historical,
                observations=(observation,),
            )
            object.__setattr__(
                observation.response_binding,
                "response_sha256",
                "sha256:" + "f" * 64,
            )
            with self.assertRaisesRegex(
                ProviderAccountSurfaceFactsError,
                "source authority changed",
            ):
                require_provider_account_surface_fact_scan_authority(value)


if __name__ == "__main__":
    unittest.main()
