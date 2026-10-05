from datetime import datetime, timedelta, timezone
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.dispatch import (
    _envelope,
    _identity_digest,
    stable_client_order_id,
    submission_attempt_aggregate_id,
)
from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_account_absence_coverage import (
    HistoricalUnknownSubmissionBinding,
    ProviderAccountAbsenceCoverageError,
    ProviderAccountSurfaceCoverage,
    issue_provider_account_surface_coverage,
    require_current_provider_account_surface_coverage_authority,
    require_historical_unknown_submission_authority,
    require_provider_account_surface_coverage_authority,
    resolve_historical_unknown_submission,
)
from mvp.autotrade_mvp.provider_account_absence_semantics import (
    resolve_current_provider_account_absence_semantics,
)
from mvp.autotrade_mvp.provider_account_acquisition import (
    DurableProviderAccountAcquisitionAuthority,
)
from mvp.autotrade_mvp.provider_account_origin_set import (
    issue_provider_account_origin_set,
)
from mvp.autotrade_mvp.provider_account_page_chain import (
    issue_provider_account_page_chain,
)
from mvp.autotrade_mvp.provider_account_reconciliation_semantics import (
    resolve_current_provider_account_reconciliation_semantics,
)
from mvp.autotrade_mvp.provider_core import Surface
from mvp.autotrade_mvp.provider_origin import (
    ProviderOriginJournal,
    observe_provider_origin_json_response,
)
from mvp.autotrade_mvp.provider_route_reads import (
    prepare_qualified_provider_read,
)
from mvp.autotrade_mvp.provider_selection import select_provider
from mvp.tests.provider_qualification_test_support import (
    ExactQualificationProjectionHarness,
)
from mvp.tests.test_provider_account_origin_set import (
    ProviderAccountOriginSetTests,
)
from mvp.tests.test_provider_account_page_chain import (
    ENDPOINT,
    SURFACE,
    _absence_claims,
)
from mvp.tests.test_provider_route_reads import verified_read_capability
from mvp.tests.test_provider_selection import (
    NOW,
    accepted_spot_q,
    candidate,
    request as route_request,
)


INTENT_ID = "coverage-intent-1"
ATTEMPT_ID = "coverage-attempt-1"


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _milliseconds(value: datetime) -> str:
    point = value.astimezone(timezone.utc)
    delta = point - datetime(1970, 1, 1, tzinfo=timezone.utc)
    milliseconds = (
        delta.days * 86_400_000
        + delta.seconds * 1000
        + delta.microseconds // 1000
    )
    return str(milliseconds)


def _append_unknown(
    store: JournalStore,
    *,
    environment: str,
    account_id: str,
    provider: str,
    intent_id: str,
    attempt_id: str,
    client_order_id: str,
    sent_at: datetime,
    with_sending: bool = True,
) -> None:
    scope_key = _identity_digest(environment, account_id)
    aggregate_id = submission_attempt_aggregate_id(
        environment=environment,
        account_id=account_id,
        attempt_id=attempt_id,
    )
    owner_token = "coverage-owner-token"
    owner_epoch = 1
    prepared_at = sent_at - timedelta(seconds=1)
    prepared_payload = {
        "attempt_id": attempt_id,
        "intent_id": intent_id,
        "intent_hash": "intent-hash-for-coverage-test",
        "provider": provider,
        "request_hash": "sha256:" + "a" * 64,
        "client_order_id": client_order_id,
        "environment": environment,
        "account_id": account_id,
        "owner_token": owner_token,
        "owner_epoch": owner_epoch,
        "prepared_at": _utc(prepared_at),
        "submission_scope": {},
        "submission_scope_hash": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    }
    JournalStore.append_event(
        store,
        _envelope(
            scope_key=scope_key,
            aggregate_id=aggregate_id,
            environment=environment,
            attempt_id=attempt_id,
            event_type="SubmissionPrepared",
            version=1,
            payload=prepared_payload,
            now=_utc(prepared_at),
            owner_epoch=owner_epoch,
        ),
    )
    if not with_sending:
        JournalStore.append_event(
            store,
            _envelope(
                scope_key=scope_key,
                aggregate_id=aggregate_id,
                environment=environment,
                attempt_id=attempt_id,
                event_type="SubmissionUnknown",
                version=2,
                payload={
                    "client_order_id": client_order_id,
                    "reason": "provider_wrapper_returned_without_final_guard",
                },
                now=_utc(sent_at),
                owner_epoch=owner_epoch,
            ),
        )
        return

    JournalStore.append_event(
        store,
        _envelope(
            scope_key=scope_key,
            aggregate_id=aggregate_id,
            environment=environment,
            attempt_id=attempt_id,
            event_type="SubmissionSending",
            version=2,
            payload={
                "client_order_id": client_order_id,
                "owner_token": owner_token,
                "owner_epoch": owner_epoch,
                "reason": "final_send_barrier_passed",
            },
            now=_utc(sent_at),
            owner_epoch=owner_epoch,
        ),
    )
    JournalStore.append_event(
        store,
        _envelope(
            scope_key=scope_key,
            aggregate_id=aggregate_id,
            environment=environment,
            attempt_id=attempt_id,
            event_type="SubmissionUnknown",
            version=3,
            payload={
                "client_order_id": client_order_id,
                "reason": "transport_exception_after_send_barrier:TimeoutError",
            },
            now=_utc(sent_at + timedelta(seconds=1)),
            owner_epoch=owner_epoch,
        ),
    )


class ProviderAccountAbsenceCoverageTests(unittest.TestCase):
    def _fixture(
        self,
        directory: str,
        *,
        query_builder=None,
        historical_environment: str | None = None,
        with_sending: bool = True,
    ):
        journal = JournalStore(Path(directory) / "journal.sqlite3")
        capabilities = DurableCapabilityRegistry(journal)
        capabilities.add(
            verified_read_capability(
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                NOW - timedelta(minutes=1),
                permission_scopes=frozenset(
                    {"ORDER.READ", "ORDER.WRITE", "ACCOUNT.READ"}
                ),
                data_entitlements=frozenset(
                    {"QUOTE", "BALANCES", "ORDERS", "EXECUTIONS", "ACTIVITIES"}
                ),
            )
        )
        evidence_root = Path(directory) / "evidence"
        harness = ExactQualificationProjectionHarness().start()
        self.addCleanup(harness.stop)
        qualifications = harness.registry(
            journal,
            evidence_store=ArtifactStore(evidence_root),
            evidence_root=evidence_root,
        )
        record, receipt, protocol = accepted_spot_q(
            ordinal=94,
            include_read_rule=False,
            extra_route_semantics=_absence_claims(),
        )
        harness.register(
            protocol_key=protocol.key,
            record=record,
            receipt=receipt,
        )
        qualifications._append_accepted(
            protocol_key=protocol.key,
            record=record,
            receipt=receipt,
        )
        selection = select_provider(
            route_request(),
            [candidate()],
            at=NOW,
            capability_registry=capabilities,
            qualification_registry=qualifications,
        )
        self.assertEqual(selection.status, "SELECTED_UNAMBIGUOUS")
        route = selection.selected
        self.assertIsNotNone(route)
        account_id = route.candidate.account_id
        runtime_environment = record.scope.provider_scope.runtime_environment
        client_order_id = stable_client_order_id(
            "BYBIT",
            INTENT_ID,
            environment=runtime_environment,
            account_id=account_id,
        )
        send_at = NOW - timedelta(minutes=10)
        resolved_environment = (
            runtime_environment
            if historical_environment is None
            else historical_environment
        )
        _append_unknown(
            journal,
            environment=resolved_environment,
            account_id=account_id,
            provider="BYBIT",
            intent_id=INTENT_ID,
            attempt_id=ATTEMPT_ID,
            client_order_id=client_order_id,
            sent_at=send_at,
            with_sending=with_sending,
        )

        historical = None
        if with_sending:
            historical = resolve_historical_unknown_submission(
                journal,
                environment=resolved_environment,
                account_id=account_id,
                attempt_id=ATTEMPT_ID,
            )

        if query_builder is None:
            query = {
                "category": "spot",
                "orderLinkId": client_order_id,
                "startTime": _milliseconds(send_at - timedelta(minutes=1)),
                "endTime": _milliseconds(NOW - timedelta(minutes=1)),
                "limit": "100",
            }
        else:
            query = query_builder(
                client_order_id=client_order_id,
                send_at=send_at,
            )
        binding = prepare_qualified_provider_read(
            route,
            capabilities,
            qualifications,
            surface=Surface.AUTHENTICATED_READ,
            endpoint=ENDPOINT,
            query=query,
            at=NOW,
            permission_scope="ORDER.READ",
        )
        origin = ProviderOriginJournal(
            journal,
            response_store=ArtifactStore(
                Path(directory) / "provider-origin-artifacts"
            ),
        )
        acquisition_authority = DurableProviderAccountAcquisitionAuthority(journal)
        acquisition = acquisition_authority.issue_serialized(
            provider_scope=record.scope.provider_scope,
            account_id=binding.query_binding.account_id,
            acquisition_request_id="absence-coverage-acquisition-1",
            committed_at=NOW,
        )
        origin_fixture = (
            None,
            journal,
            origin,
            qualifications,
            acquisition_authority,
            acquisition,
            binding,
        )
        response = ProviderAccountOriginSetTests._direct_binding(
            self,
            origin_fixture,
            directory,
            marker="absence-coverage-root",
        )
        observation = observe_provider_origin_json_response(
            response_binding=response,
            query_binding=binding,
        )
        origin_set = issue_provider_account_origin_set(
            qualification_registry=qualifications,
            account_acquisition_authority=acquisition_authority,
            account_acquisition=acquisition,
            response_bindings=(response,),
            at=NOW,
        )
        reconciliation = resolve_current_provider_account_reconciliation_semantics(
            qualification_registry=qualifications,
            qualification_id=record.qualification_id,
            provider_scope_digest=record.scope.provider_scope.content_digest,
            at=NOW,
        )
        absence = resolve_current_provider_account_absence_semantics(
            reconciliation_semantics=reconciliation,
            qualification_registry=qualifications,
            at=NOW,
        )
        page_chain = issue_provider_account_page_chain(
            absence_semantics=absence,
            origin_set=origin_set,
            observations=(observation,),
            qualification_registry=qualifications,
            surface=SURFACE,
            at=NOW,
        )
        return (
            journal,
            qualifications,
            absence,
            page_chain,
            historical,
            account_id,
            runtime_environment,
            client_order_id,
            send_at,
        )

    def test_resolves_exact_durable_possible_send_unknown(self):
        with TemporaryDirectory() as directory:
            (
                _journal,
                _qualifications,
                _absence,
                _page_chain,
                historical,
                _account_id,
                _environment,
                client_order_id,
                _send_at,
            ) = self._fixture(directory)
            self.assertIsInstance(
                historical,
                HistoricalUnknownSubmissionBinding,
            )
            self.assertEqual(historical.provider_id, "BYBIT")
            self.assertEqual(historical.client_order_id, client_order_id)
            self.assertTrue(
                historical.content_digest.startswith(
                    "historical-unknown-submission:sha256:"
                )
            )
            require_historical_unknown_submission_authority(historical)

    def test_resolver_accepts_no_caller_financial_identity_fields(self):
        parameters = inspect.signature(
            resolve_historical_unknown_submission
        ).parameters
        for forbidden in (
            "provider_id",
            "client_order_id",
            "intent_id",
            "started_at",
            "request_hash",
            "submission_scope_hash",
            "sent",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)

    def test_guardless_unknown_is_not_possible_send_authority(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.sqlite3")
            environment = "PAPER"
            account_id = "paper-account"
            client_order_id = stable_client_order_id(
                "BYBIT",
                INTENT_ID,
                environment=environment,
                account_id=account_id,
            )
            _append_unknown(
                journal,
                environment=environment,
                account_id=account_id,
                provider="BYBIT",
                intent_id=INTENT_ID,
                attempt_id=ATTEMPT_ID,
                client_order_id=client_order_id,
                sent_at=NOW - timedelta(minutes=10),
                with_sending=False,
            )
            with self.assertRaisesRegex(
                ProviderAccountAbsenceCoverageError,
                "Prepared -> Sending -> Unknown",
            ):
                resolve_historical_unknown_submission(
                    journal,
                    environment=environment,
                    account_id=account_id,
                    attempt_id=ATTEMPT_ID,
                )

    def test_exact_client_window_issues_surface_coverage_without_booleans(self):
        with TemporaryDirectory() as directory:
            (
                _journal,
                qualifications,
                absence,
                page_chain,
                historical,
                _account_id,
                _environment,
                _client_order_id,
                _send_at,
            ) = self._fixture(directory)
            value = issue_provider_account_surface_coverage(
                absence_semantics=absence,
                page_chain=page_chain,
                historical_submission=historical,
                qualification_registry=qualifications,
                at=NOW,
            )
            self.assertIsInstance(value, ProviderAccountSurfaceCoverage)
            self.assertEqual(value.surface, "EXECUTIONS")
            self.assertEqual(
                value.search_binding,
                "EXACT_CLIENT_ORDER_ID_WINDOW",
            )
            self.assertEqual(value.page_count, 1)
            self.assertEqual(
                value.retention_rule_id,
                "BYBIT_EXECUTION_RETENTION_V1",
            )
            self.assertTrue(
                value.content_digest.startswith(
                    "provider-account-surface-coverage:sha256:"
                )
            )
            require_provider_account_surface_coverage_authority(value)

    def test_surface_coverage_rejects_acquisition_superseded_after_issuance(self):
        with TemporaryDirectory() as directory:
            (
                journal,
                qualifications,
                absence,
                page_chain,
                historical,
                *_rest,
            ) = self._fixture(directory)
            value = issue_provider_account_surface_coverage(
                absence_semantics=absence,
                page_chain=page_chain,
                historical_submission=historical,
                qualification_registry=qualifications,
                at=NOW,
            )
            provider_scope = qualifications.qualification(
                page_chain.qualification_id
            ).scope.provider_scope
            DurableProviderAccountAcquisitionAuthority(journal).issue_serialized(
                provider_scope=provider_scope,
                account_id=page_chain.account_id,
                acquisition_request_id="absence-coverage-acquisition-after-issuance",
                committed_at=NOW,
            )

            require_provider_account_surface_coverage_authority(value)
            with self.assertRaisesRegex(
                ProviderAccountAbsenceCoverageError,
                "not exact current authority",
            ):
                require_current_provider_account_surface_coverage_authority(
                    value,
                    at=NOW,
                )
            with self.assertRaisesRegex(
                ProviderAccountAbsenceCoverageError,
                "source authority is unavailable",
            ):
                issue_provider_account_surface_coverage(
                    absence_semantics=absence,
                    page_chain=page_chain,
                    historical_submission=historical,
                    qualification_registry=qualifications,
                    at=NOW,
                )

    def test_surface_issuer_has_no_caller_completeness_or_horizon_flags(self):
        parameters = inspect.signature(
            issue_provider_account_surface_coverage
        ).parameters
        for forbidden in (
            "pagination_complete",
            "consistency_horizon_satisfied",
            "provider_semantics_exclude_execution",
            "searched_client_order_ids",
            "coverage_start",
            "coverage_end",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, parameters)

    def test_wrong_client_order_search_is_rejected(self):
        with TemporaryDirectory() as directory:
            (
                _journal,
                qualifications,
                absence,
                page_chain,
                historical,
                *_rest,
            ) = self._fixture(
                directory,
                query_builder=lambda *, client_order_id, send_at: {
                    "category": "spot",
                    "orderLinkId": client_order_id + "-wrong",
                    "startTime": _milliseconds(
                        send_at - timedelta(minutes=1)
                    ),
                    "endTime": _milliseconds(NOW - timedelta(minutes=1)),
                    "limit": "100",
                },
            )
            with self.assertRaisesRegex(
                ProviderAccountAbsenceCoverageError,
                "exact client order id",
            ):
                issue_provider_account_surface_coverage(
                    absence_semantics=absence,
                    page_chain=page_chain,
                    historical_submission=historical,
                    qualification_registry=qualifications,
                    at=NOW,
                )

    def test_implicit_default_time_window_is_not_absence_coverage(self):
        with TemporaryDirectory() as directory:
            (
                _journal,
                qualifications,
                absence,
                page_chain,
                historical,
                *_rest,
            ) = self._fixture(
                directory,
                query_builder=lambda *, client_order_id, send_at: {
                    "category": "spot",
                    "orderLinkId": client_order_id,
                    "limit": "100",
                },
            )
            with self.assertRaisesRegex(
                ProviderAccountAbsenceCoverageError,
                "explicit startTime and endTime",
            ):
                issue_provider_account_surface_coverage(
                    absence_semantics=absence,
                    page_chain=page_chain,
                    historical_submission=historical,
                    qualification_registry=qualifications,
                    at=NOW,
                )

    def test_query_window_must_contain_possible_send(self):
        with TemporaryDirectory() as directory:
            (
                _journal,
                qualifications,
                absence,
                page_chain,
                historical,
                *_rest,
            ) = self._fixture(
                directory,
                query_builder=lambda *, client_order_id, send_at: {
                    "category": "spot",
                    "orderLinkId": client_order_id,
                    "startTime": _milliseconds(NOW - timedelta(minutes=5)),
                    "endTime": _milliseconds(NOW - timedelta(minutes=1)),
                    "limit": "100",
                },
            )
            with self.assertRaisesRegex(
                ProviderAccountAbsenceCoverageError,
                "outside provider query window",
            ):
                issue_provider_account_surface_coverage(
                    absence_semantics=absence,
                    page_chain=page_chain,
                    historical_submission=historical,
                    qualification_registry=qualifications,
                    at=NOW,
                )

    def test_historical_unknown_runtime_must_match_current_q(self):
        with TemporaryDirectory() as directory:
            (
                _journal,
                qualifications,
                absence,
                page_chain,
                historical,
                *_rest,
            ) = self._fixture(
                directory,
                historical_environment="LIVE",
            )
            with self.assertRaisesRegex(
                ProviderAccountAbsenceCoverageError,
                "runtime scope",
            ):
                issue_provider_account_surface_coverage(
                    absence_semantics=absence,
                    page_chain=page_chain,
                    historical_submission=historical,
                    qualification_registry=qualifications,
                    at=NOW,
                )

    def test_post_issue_source_mutation_invalidates_coverage_authority(self):
        with TemporaryDirectory() as directory:
            (
                _journal,
                qualifications,
                absence,
                page_chain,
                historical,
                *_rest,
            ) = self._fixture(directory)
            value = issue_provider_account_surface_coverage(
                absence_semantics=absence,
                page_chain=page_chain,
                historical_submission=historical,
                qualification_registry=qualifications,
                at=NOW,
            )
            object.__setattr__(historical, "client_order_id", "mutated")
            with self.assertRaisesRegex(
                ProviderAccountAbsenceCoverageError,
                "source authority changed",
            ):
                require_provider_account_surface_coverage_authority(value)


if __name__ == "__main__":
    unittest.main()
