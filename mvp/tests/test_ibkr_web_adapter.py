from functools import partial
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from types import MappingProxyType
import json
import unittest

import mvp.autotrade_mvp.ibkr_web as ibkr_web_module
import mvp.autotrade_mvp.provider_core as provider_core_module
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    CapabilitySnapshot,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    ProviderResponseObservation,
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.ibkr_web import (
    IBKR_WEB_DOCS,
    IbkrAbsenceEvidence,
    IbkrBrokerageAccountsObservation,
    IbkrBrokerageSessionStatus,
    IbkrCancelRequest,
    IbkrContractIdentity,
    IbkrExecutionEvidence,
    IbkrNormalizedOrder,
    IbkrReplyRequest,
    IbkrWebAdapterError,
    IbkrWebOrderIntent,
    brokerage_accounts_from_observation,
    brokerage_session_status_from_observation,
    execution_to_reconciliation_fill,
    parse_cancel_response,
    parse_order_submission_response,
    prepare_cancel_request,
    parse_web_api_trades,
    prepare_normalized_order,
    prepare_reply_confirmation,
    record_order_submission_result,
)

from mvp.tests.capability_test_support import fresh_test_admission

NOW = datetime(2026, 9, 24, 20, tzinfo=timezone.utc)
EXECUTION_EVIDENCE_REFS = ("ibkr-execution-evidence:test",)


class _HostileText(str):
    strip_called = False

    def strip(self, *args, **kwargs):
        type(self).strip_called = True
        raise AssertionError("hostile text callback executed")


class _HostileInt(int):
    comparison_called = False

    def __le__(self, other):
        type(self).comparison_called = True
        raise AssertionError("hostile integer comparison executed")

    def __lt__(self, other):
        type(self).comparison_called = True
        raise AssertionError("hostile integer comparison executed")


class _HostileDecimal(Decimal):
    def is_finite(self):
        raise AssertionError("hostile Decimal callback executed")

    def as_tuple(self):
        raise AssertionError("hostile Decimal callback executed")


class _HostileDatetime(datetime):
    def utcoffset(self):
        raise AssertionError("hostile datetime callback executed")

    def astimezone(self, *args, **kwargs):
        raise AssertionError("hostile datetime callback executed")


class _HostileTimezone(tzinfo):
    def utcoffset(self, dt):
        raise AssertionError("hostile timezone callback executed")

    def dst(self, dt):
        return None


def capability(
    *,
    account_id="U1234567",
    environment="PAPER",
    order_types=("MARKET", "LIMIT", "STOP", "STOP_LIMIT"),
):
    observed_at = NOW - timedelta(hours=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="IBKR",
            account_id=account_id,
            entity_id="web-api",
            environment=environment,
            instrument_version="AAPL-CONID-265598:v1",
            observed_at=observed_at,
            expires_at=NOW + timedelta(hours=1),
            supported_order_types=frozenset(order_types),
            time_in_force=frozenset({"DAY", "GTC", "IOC"}),
            permission_scopes=frozenset({"ORDER_WRITE", "ORDER.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="ibkr-web-paper",
            data_entitlements=frozenset({"ORDERS", "EXECUTIONS"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "e" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://www.interactivebrokers.com/docs/web-api/v1/endpoints/orders/place-order",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return fresh_test_admission(derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=observed_at,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    ))


def ibkr_session_observation(
    payload,
    *,
    account_id="U1234567",
    environment="PAPER",
    observed_at=None,
    endpoint="/iserver/auth/status",
    query=None,
    permission_scope="ORDER.READ",
    documented_envelope=True,
):
    point = NOW - timedelta(seconds=1) if observed_at is None else observed_at
    binding = prepare_authenticated_read_query(
        capability=capability(
            account_id=account_id,
            environment=environment,
        ),
        surface=Surface.AUTHENTICATED_READ,
        endpoint=endpoint,
        query={} if query is None else query,
        at=point,
        permission_scope=permission_scope,
    )
    return observe_authenticated_json_response(
        query_binding=binding,
        http_status=200,
        response_bytes=json.dumps(
            (
                {"success": {"value": payload}}
                if documented_envelope
                else payload
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        observed_at=point,
    )


def ready_session(*, account_id="U1234567", environment="PAPER", **overrides):
    values = dict(
        connected=True,
        authenticated=True,
        established=True,
        competing=False,
        observed_at=NOW - timedelta(seconds=1),
    )
    values.update(overrides)
    observed_at = values.pop("observed_at")
    return brokerage_session_status_from_observation(
        ibkr_session_observation(
            values,
            account_id=account_id,
            environment=environment,
            observed_at=observed_at,
        )
    )


def ibkr_accounts_observation(
    payload,
    *,
    account_id="U1234567",
    environment="PAPER",
    observed_at=None,
    endpoint="/iserver/accounts",
    query=None,
    permission_scope="ORDER.READ",
):
    point = NOW if observed_at is None else observed_at
    binding = prepare_authenticated_read_query(
        capability=capability(
            account_id=account_id,
            environment=environment,
        ),
        surface=Surface.AUTHENTICATED_READ,
        endpoint=endpoint,
        query={} if query is None else query,
        at=point,
        permission_scope=permission_scope,
    )
    return observe_authenticated_json_response(
        query_binding=binding,
        http_status=200,
        response_bytes=json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        observed_at=point,
    )


def ready_accounts(
    *,
    account_id="U1234567",
    environment="PAPER",
    observed_at=None,
    accounts=None,
    selected_account=None,
    session_id="ibkr-session-1",
    is_paper=None,
):
    provider_accounts = (
        [account_id]
        if accounts is None
        else accounts
    )
    selected = account_id if selected_account is None else selected_account
    paper = (environment == "PAPER") if is_paper is None else is_paper
    return brokerage_accounts_from_observation(
        ibkr_accounts_observation(
            {
                "accounts": provider_accounts,
                "selectedAccount": selected,
                "sessionId": session_id,
                "isPaper": paper,
                "serverInfo": {
                    "serverName": "JifN19053",
                    "serverVersion": "Build test",
                },
            },
            account_id=account_id,
            environment=environment,
            observed_at=NOW if observed_at is None else observed_at,
        )
    )


_product_prepare_normalized_order = prepare_normalized_order


def prepare_normalized_order(
    *args,
    accounts=None,
    maximum_accounts_age_seconds=30,
    **kwargs,
):
    if accounts is None:
        accounts = ready_accounts()
    return _product_prepare_normalized_order(
        *args,
        accounts=accounts,
        maximum_accounts_age_seconds=maximum_accounts_age_seconds,
        **kwargs,
    )


def ibkr_trade_observation(payload, *, account_id="U1234567"):
    binding = prepare_authenticated_read_query(
        capability=capability(account_id=account_id),
        surface=Surface.ACTIVITIES,
        endpoint="/iserver/account/trades",
        query={},
        at=NOW,
    )
    return observe_authenticated_json_response(
        query_binding=binding,
        http_status=200,
        response_bytes=json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8"),
        observed_at=NOW,
    )


# Bind only the non-provider-read fixture helper.
execution_to_reconciliation_fill = partial(
    execution_to_reconciliation_fill,
    environment="PAPER",
)


class IbkrWebAdapterTests(unittest.TestCase):
    def test_brokerage_accounts_are_source_bound_and_capture_provider_session(self):
        observed = ready_accounts()
        self.assertEqual(observed.accounts, ("U1234567",))
        self.assertEqual(observed.selected_account, "U1234567")
        self.assertEqual(observed.session_id, "ibkr-session-1")
        self.assertIs(observed.is_paper, True)
        self.assertEqual(observed.observed_at, NOW)

    def test_brokerage_accounts_reject_local_forgery_as_financial_authority(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        forged = IbkrBrokerageAccountsObservation(
            accounts=("U1234567",),
            selected_account="U1234567",
            session_id="forged-session",
            is_paper=True,
            observed_at=NOW,
        )
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "/iserver/accounts provider observation",
        ):
            _product_prepare_normalized_order(
                intent,
                client_order_id="at-forged-accounts",
                capability=capability(),
                session=ready_session(),
                accounts=forged,
                at=NOW,
                maximum_session_age_seconds=30,
                maximum_accounts_age_seconds=30,
            )

    def test_brokerage_accounts_use_sealed_provider_response_authority(self):
        observation = ibkr_accounts_observation(
            {
                "accounts": ["U1234567", "FORGED"],
                "selectedAccount": "U1234567",
                "sessionId": "session-1",
                "isPaper": True,
            }
        )
        object.__setattr__(observation.query_binding, "account_id", "FORGED")

        original_response_authority = (
            provider_core_module._require_provider_response_observation_authority
        )
        original_query_authority = (
            provider_core_module._require_authenticated_read_query_binding_authority
        )
        original_scope = (
            provider_core_module.AuthenticatedReadQueryBinding.require_scope
        )
        provider_core_module._require_provider_response_observation_authority = (
            lambda *_args, **_kwargs: None
        )
        provider_core_module._require_authenticated_read_query_binding_authority = (
            lambda *_args, **_kwargs: None
        )
        provider_core_module.AuthenticatedReadQueryBinding.require_scope = (
            lambda *_args, **_kwargs: None
        )
        try:
            with self.assertRaisesRegex(
                ProviderCoreError,
                "authenticated-read binding changed after preparation",
            ):
                brokerage_accounts_from_observation(observation)
        finally:
            provider_core_module._require_provider_response_observation_authority = (
                original_response_authority
            )
            provider_core_module._require_authenticated_read_query_binding_authority = (
                original_query_authority
            )
            provider_core_module.AuthenticatedReadQueryBinding.require_scope = (
                original_scope
            )

    def test_brokerage_accounts_require_exact_endpoint_empty_query_and_read_scope(self):
        payload = {
            "accounts": ["U1234567"],
            "selectedAccount": "U1234567",
            "sessionId": "session-1",
            "isPaper": True,
        }
        with self.assertRaisesRegex(ProviderCoreError, "endpoint mismatch"):
            brokerage_accounts_from_observation(
                ibkr_accounts_observation(
                    payload,
                    endpoint="/iserver/auth/status",
                )
            )
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "empty authenticated query",
        ):
            brokerage_accounts_from_observation(
                ibkr_accounts_observation(
                    payload,
                    query={"caller": "selected"},
                )
            )
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "ORDER.READ scope",
        ):
            brokerage_accounts_from_observation(
                ibkr_accounts_observation(
                    payload,
                    permission_scope="ORDER_WRITE",
                )
            )

    def test_brokerage_accounts_reject_malformed_membership_and_environment(self):
        base = {
            "accounts": ["U1234567"],
            "selectedAccount": "U1234567",
            "sessionId": "session-1",
            "isPaper": True,
        }
        cases = (
            (dict(base, accounts=[]), "non-empty array"),
            (
                dict(base, accounts=["U1234567", "U1234567"]),
                "duplicate account ids",
            ),
            (
                dict(base, selectedAccount="OTHER"),
                "selected brokerage account is absent",
            ),
            (dict(base, sessionId=" session-1 "), "session id"),
            (dict(base, isPaper=1), "isPaper must be exact boolean"),
        )
        for payload, message in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(IbkrWebAdapterError, message):
                    brokerage_accounts_from_observation(
                        ibkr_accounts_observation(payload)
                    )

        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "isPaper does not match authenticated environment",
        ):
            brokerage_accounts_from_observation(
                ibkr_accounts_observation(
                    dict(base, isPaper=False),
                    environment="PAPER",
                )
            )
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "scope is absent from provider accounts",
        ):
            brokerage_accounts_from_observation(
                ibkr_accounts_observation(
                    {
                        "accounts": ["OTHER"],
                        "selectedAccount": "OTHER",
                        "sessionId": "session-1",
                        "isPaper": True,
                    },
                    account_id="U1234567",
                )
            )

    def test_order_preparation_requires_current_post_status_account_membership(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        session = ready_session(observed_at=NOW - timedelta(seconds=1))

        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "predates authenticated session status",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-predating-accounts",
                capability=capability(),
                session=session,
                accounts=ready_accounts(
                    observed_at=NOW - timedelta(seconds=2),
                ),
                at=NOW,
                maximum_session_age_seconds=30,
                maximum_accounts_age_seconds=30,
            )

        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "brokerage accounts evidence is from the future",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-future-accounts",
                capability=capability(),
                session=session,
                accounts=ready_accounts(
                    observed_at=NOW + timedelta(seconds=1),
                ),
                at=NOW,
                maximum_session_age_seconds=30,
                maximum_accounts_age_seconds=30,
            )

        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "brokerage accounts evidence is stale",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-stale-accounts",
                capability=capability(),
                session=ready_session(
                    observed_at=NOW - timedelta(seconds=40),
                ),
                accounts=ready_accounts(
                    observed_at=NOW - timedelta(seconds=31),
                ),
                at=NOW,
                maximum_session_age_seconds=60,
                maximum_accounts_age_seconds=30,
            )

    def test_order_preparation_rejects_cross_environment_and_mutated_accounts(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "accounts environment",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-cross-env-accounts",
                capability=capability(),
                session=ready_session(),
                accounts=ready_accounts(environment="LIVE"),
                at=NOW,
                maximum_session_age_seconds=30,
                maximum_accounts_age_seconds=30,
            )

        observed = ready_accounts()
        object.__setattr__(observed, "session_id", "mutated-session")
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "changed after authenticated provider observation",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-mutated-accounts",
                capability=capability(),
                session=ready_session(),
                accounts=observed,
                at=NOW,
                maximum_session_age_seconds=30,
                maximum_accounts_age_seconds=30,
            )

    def test_accounts_freshness_policy_is_exact_non_negative_integer(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        for invalid in (True, -1):
            with self.subTest(maximum_accounts_age_seconds=invalid):
                with self.assertRaisesRegex(
                    IbkrWebAdapterError,
                    "maximum_accounts_age_seconds must be a non-negative integer",
                ):
                    prepare_normalized_order(
                        intent,
                        client_order_id="at-invalid-accounts-age",
                        capability=capability(),
                        session=ready_session(),
                        at=NOW,
                        maximum_session_age_seconds=30,
                        maximum_accounts_age_seconds=invalid,
                    )

        _HostileInt.comparison_called = False
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "maximum_accounts_age_seconds must be a non-negative integer",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-hostile-accounts-age",
                capability=capability(),
                session=ready_session(),
                at=NOW,
                maximum_session_age_seconds=30,
                maximum_accounts_age_seconds=_HostileInt(30),
            )
        self.assertFalse(_HostileInt.comparison_called)

    def test_mutated_accounts_tuple_rejects_polymorphic_members_before_comparison(self):
        class HostileAccount(str):
            compared = False

            def __eq__(self, other):
                type(self).compared = True
                raise AssertionError("hostile account equality executed")

        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        accounts = ready_accounts()
        object.__setattr__(
            accounts,
            "accounts",
            (HostileAccount("U1234567"),),
        )
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "changed after authenticated provider observation",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-hostile-accounts-member",
                capability=capability(),
                session=ready_session(),
                accounts=accounts,
                at=NOW,
                maximum_session_age_seconds=30,
                maximum_accounts_age_seconds=30,
            )
        self.assertFalse(HostileAccount.compared)

    def test_accounts_financial_guard_ignores_runtime_private_rebinding(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        forged = IbkrBrokerageAccountsObservation(
            accounts=("U1234567",),
            selected_account="U1234567",
            session_id="forged-session",
            is_paper=True,
            observed_at=NOW,
        )
        self.assertFalse(
            hasattr(
                ibkr_web_module,
                "_require_ibkr_brokerage_accounts_observation",
            )
        )
        ibkr_web_module._require_ibkr_brokerage_accounts_observation = (
            lambda *_args, **_kwargs: (
                ("U1234567",),
                "U1234567",
                "forged-session",
                True,
                NOW,
                "forged",
                "forged",
            )
        )
        try:
            with self.assertRaisesRegex(
                IbkrWebAdapterError,
                "/iserver/accounts provider observation",
            ):
                _product_prepare_normalized_order(
                    intent,
                    client_order_id="at-runtime-accounts-rebind",
                    capability=capability(),
                    session=ready_session(),
                    accounts=forged,
                    at=NOW,
                    maximum_session_age_seconds=30,
                    maximum_accounts_age_seconds=30,
                )
        finally:
            delattr(
                ibkr_web_module,
                "_require_ibkr_brokerage_accounts_observation",
            )

    def test_trade_session_requires_all_ready_flags_and_no_competitor(self):
        self.assertTrue(ready_session().trade_ready)
        for override in (
            {"connected": False},
            {"authenticated": False},
            {"established": False},
            {"competing": True},
        ):
            with self.subTest(override=override):
                with self.assertRaises(IbkrWebAdapterError):
                    ready_session(**override).require_trade_ready()

    def test_order_preparation_rejects_locally_forged_ready_session(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        forged = IbkrBrokerageSessionStatus(
            connected=True,
            authenticated=True,
            established=True,
            competing=False,
            observed_at=NOW - timedelta(seconds=1),
        )
        self.assertTrue(forged.trade_ready)
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "/iserver/auth/status provider observation",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-forged-session",
                capability=capability(),
                session=forged,
                at=NOW,
                maximum_session_age_seconds=30,
            )

    def test_session_financial_guard_ignores_runtime_private_rebinding(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        forged = IbkrBrokerageSessionStatus(
            connected=True,
            authenticated=True,
            established=True,
            competing=False,
            observed_at=NOW - timedelta(seconds=1),
        )
        self.assertFalse(
            hasattr(
                ibkr_web_module,
                "_require_ibkr_brokerage_session_observation",
            )
        )
        ibkr_web_module._require_ibkr_brokerage_session_observation = (
            lambda *_args, **_kwargs: None
        )
        try:
            with self.assertRaisesRegex(
                IbkrWebAdapterError,
                "/iserver/auth/status provider observation",
            ):
                prepare_normalized_order(
                    intent,
                    client_order_id="at-runtime-session-rebind",
                    capability=capability(),
                    session=forged,
                    at=NOW,
                    maximum_session_age_seconds=30,
                )
        finally:
            delattr(
                ibkr_web_module,
                "_require_ibkr_brokerage_session_observation",
            )

    def test_session_financial_guard_does_not_delegate_readiness_to_mutable_class_method(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        session = ready_session(connected=False)
        original = IbkrBrokerageSessionStatus.require_trade_ready
        IbkrBrokerageSessionStatus.require_trade_ready = lambda _self: None
        try:
            with self.assertRaisesRegex(
                IbkrWebAdapterError,
                "brokerage session is disconnected",
            ):
                prepare_normalized_order(
                    intent,
                    client_order_id="at-class-readiness-rebind",
                    capability=capability(),
                    session=session,
                    at=NOW,
                    maximum_session_age_seconds=30,
                )
        finally:
            IbkrBrokerageSessionStatus.require_trade_ready = original

    def test_session_parser_rejects_runtime_endpoint_authority_rebinding(self):
        observation = ibkr_session_observation(
            {
                "connected": True,
                "authenticated": True,
                "established": True,
                "competing": False,
            }
        )
        original = ibkr_web_module.IBKR_WEB_BROKERAGE_STATUS_ENDPOINT
        ibkr_web_module.IBKR_WEB_BROKERAGE_STATUS_ENDPOINT = "/iserver/accounts"
        try:
            with self.assertRaisesRegex(
                IbkrWebAdapterError,
                "session observation authority changed",
            ):
                brokerage_session_status_from_observation(observation)
        finally:
            ibkr_web_module.IBKR_WEB_BROKERAGE_STATUS_ENDPOINT = original

    def test_brokerage_status_requires_exact_authenticated_read_scope(self):
        observation = ibkr_session_observation(
            {
                "connected": True,
                "authenticated": True,
                "established": True,
                "competing": False,
            },
            endpoint="/iserver/accounts",
        )
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "observation scope mismatch",
        ):
            brokerage_session_status_from_observation(observation)

    def test_brokerage_status_scope_does_not_delegate_to_mutable_observation_method(self):
        observation = ibkr_session_observation(
            {
                "connected": True,
                "authenticated": True,
                "established": True,
                "competing": False,
            },
            endpoint="/iserver/accounts",
        )
        original = ProviderResponseObservation.require_scope
        ProviderResponseObservation.require_scope = lambda *_args, **_kwargs: None
        try:
            with self.assertRaisesRegex(
                IbkrWebAdapterError,
                "observation scope mismatch",
            ):
                brokerage_session_status_from_observation(observation)
        finally:
            ProviderResponseObservation.require_scope = original

    def test_brokerage_status_requires_documented_success_value_envelope(self):
        payload = {
            "connected": True,
            "authenticated": True,
            "established": True,
            "competing": False,
        }
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "documented success envelope",
        ):
            brokerage_session_status_from_observation(
                ibkr_session_observation(
                    payload,
                    documented_envelope=False,
                )
            )

        observation = ibkr_session_observation(payload)
        session = brokerage_session_status_from_observation(observation)
        self.assertTrue(session.trade_ready)

    def test_brokerage_status_requires_empty_query_and_read_scope(self):
        payload = {
            "connected": True,
            "authenticated": True,
            "established": True,
            "competing": False,
        }
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "empty authenticated query",
        ):
            brokerage_session_status_from_observation(
                ibkr_session_observation(
                    payload,
                    query={"caller": "selected"},
                )
            )
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "ORDER.READ scope",
        ):
            brokerage_session_status_from_observation(
                ibkr_session_observation(
                    payload,
                    permission_scope="ORDER_WRITE",
                )
            )

    def test_brokerage_status_rejects_malformed_flags_and_provider_failure(self):
        base = {
            "connected": True,
            "authenticated": True,
            "established": True,
            "competing": False,
        }
        malformed = dict(base, connected=1)
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "connected must be exact boolean",
        ):
            brokerage_session_status_from_observation(
                ibkr_session_observation(malformed)
            )

        missing = dict(base)
        missing.pop("authenticated")
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "missing authenticated",
        ):
            brokerage_session_status_from_observation(
                ibkr_session_observation(missing)
            )

        failed = dict(base, fail="competing brokerage session")
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "reports provider failure",
        ):
            brokerage_session_status_from_observation(
                ibkr_session_observation(failed)
            )

    def test_brokerage_status_account_and_environment_scope_are_bound(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "session account",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-cross-account-session",
                capability=capability(),
                session=ready_session(account_id="OTHER"),
                at=NOW,
                maximum_session_age_seconds=30,
            )
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "session environment",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-cross-environment-session",
                capability=capability(),
                session=ready_session(environment="LIVE"),
                at=NOW,
                maximum_session_age_seconds=30,
            )

    def test_mutated_issued_brokerage_status_loses_authority(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        session = ready_session()
        object.__setattr__(session, "connected", False)
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "changed after authenticated provider observation",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-mutated-session-evidence",
                capability=capability(),
                session=session,
                at=NOW,
                maximum_session_age_seconds=30,
            )

    def test_contract_identity_never_falls_back_to_ticker(self):
        self.assertEqual(IbkrContractIdentity(conid=265598).contract_key, "265598")
        self.assertEqual(
            IbkrContractIdentity(conidex="557335679@ZEROHASH").contract_key,
            "557335679@ZEROHASH",
        )
        with self.assertRaises(IbkrWebAdapterError):
            IbkrContractIdentity()
        with self.assertRaises(IbkrWebAdapterError):
            IbkrContractIdentity(conid=265598, conidex="265598@SMART")

    def test_contract_conid_rejects_integer_subclass_before_comparison(self):
        _HostileInt.comparison_called = False
        with self.assertRaisesRegex(
            IbkrWebAdapterError, "positive exact integer"
        ):
            IbkrContractIdentity(conid=_HostileInt(265598))
        self.assertFalse(_HostileInt.comparison_called)

    def test_direct_intent_cannot_bypass_exact_or_regulatory_invariants(self):
        contract = IbkrContractIdentity(conid=265598)
        with self.assertRaisesRegex(IbkrWebAdapterError, "exact decimal"):
            IbkrWebOrderIntent(
                instrument_version="AAPL-CONID-265598:v1",
                account_id="U1234567",
                contract=contract,
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity=1.0,
            )
        with self.assertRaisesRegex(IbkrWebAdapterError, "manual_indicator is required"):
            IbkrWebOrderIntent(
                instrument_version="AAPL-CONID-265598:v1",
                account_id="U1234567",
                contract=contract,
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity=Decimal("1"),
                regulatory_manual_indicator_required=True,
            )
        normalized = IbkrWebOrderIntent(
            instrument_version=" AAPL-CONID-265598:v1 ",
            account_id=" U1234567 ",
            contract=contract,
            side="buy",
            order_type="limit",
            time_in_force="day",
            quantity="1.25",
            limit_price="220.10",
        )
        self.assertEqual(normalized.side, "BUY")
        self.assertEqual(normalized.order_type, "LIMIT")
        self.assertEqual(normalized.quantity, Decimal("1.25"))
        self.assertEqual(normalized.limit_price, Decimal("220.10"))

    def test_intent_text_ingress_rejects_string_subclass_before_callbacks(self):
        _HostileText.strip_called = False
        with self.assertRaisesRegex(IbkrWebAdapterError, "side is required"):
            IbkrWebOrderIntent.create(
                instrument_version="AAPL-CONID-265598:v1",
                account_id="U1234567",
                contract=IbkrContractIdentity(conid=265598),
                side=_HostileText("BUY"),
                order_type="MARKET",
                time_in_force="DAY",
                quantity="1",
            )
        self.assertFalse(_HostileText.strip_called)

    def test_financial_numeric_ingress_rejects_decimal_subclass_before_callbacks(self):
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "bounded exact decimal",
        ):
            IbkrWebOrderIntent.create(
                instrument_version="AAPL-CONID-265598:v1",
                account_id="U1234567",
                contract=IbkrContractIdentity(conid=265598),
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity=_HostileDecimal("1"),
            )

    def test_session_time_rejects_datetime_subclass_before_callbacks(self):
        hostile = _HostileDatetime(2026, 9, 24, 20, tzinfo=timezone.utc)
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "exact timezone-aware datetime",
        ):
            IbkrBrokerageSessionStatus(
                connected=True,
                authenticated=True,
                established=True,
                competing=False,
                observed_at=hostile,
            )

    def test_session_time_rejects_custom_timezone_before_callbacks(self):
        hostile = datetime(2026, 9, 24, 20, tzinfo=_HostileTimezone())
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "exact timezone-aware datetime",
        ):
            IbkrBrokerageSessionStatus(
                connected=True,
                authenticated=True,
                established=True,
                competing=False,
                observed_at=hostile,
            )

    def test_order_preparation_rejects_datetime_subclass_before_callbacks(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        hostile = _HostileDatetime(2026, 9, 24, 20, tzinfo=timezone.utc)
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "exact timezone-aware datetime",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-hostile-time",
                capability=capability(),
                session=ready_session(),
                at=hostile,
                maximum_session_age_seconds=30,
            )

    def test_normalized_order_rejects_polymorphic_admission_authorities(self):
        class ExecutableIntent(IbkrWebOrderIntent):
            pass

        class ExecutableCapability(CapabilitySnapshot):
            admits_called = False

            def admits(self, **kwargs):
                type(self).admits_called = True
                raise AssertionError("capability callback executed")

        class ExecutableSession(IbkrBrokerageSessionStatus):
            readiness_called = False

            def require_trade_ready(self):
                type(self).readiness_called = True
                raise AssertionError("session callback executed")

        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        cap = capability()
        session = ready_session()

        with self.assertRaisesRegex(TypeError, "exact IbkrWebOrderIntent"):
            prepare_normalized_order(
                object.__new__(ExecutableIntent),
                client_order_id="at-polymorphic-intent",
                capability=cap,
                session=session,
                at=NOW,
                maximum_session_age_seconds=30,
            )

        with self.assertRaisesRegex(TypeError, "exact CapabilitySnapshot"):
            prepare_normalized_order(
                intent,
                client_order_id="at-polymorphic-capability",
                capability=object.__new__(ExecutableCapability),
                session=session,
                at=NOW,
                maximum_session_age_seconds=30,
            )
        self.assertFalse(ExecutableCapability.admits_called)

        with self.assertRaisesRegex(TypeError, "exact IbkrBrokerageSessionStatus"):
            prepare_normalized_order(
                intent,
                client_order_id="at-polymorphic-session",
                capability=cap,
                session=object.__new__(ExecutableSession),
                at=NOW,
                maximum_session_age_seconds=30,
            )
        self.assertFalse(ExecutableSession.readiness_called)

    def test_order_preparation_does_not_delegate_admission_to_mutable_capability_class_method(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        cap = capability(order_types=("LIMIT",))
        original = CapabilitySnapshot.admits
        CapabilitySnapshot.admits = lambda *_args, **_kwargs: True
        try:
            with self.assertRaisesRegex(
                IbkrWebAdapterError,
                "exact capability evidence does not admit this order",
            ):
                prepare_normalized_order(
                    intent,
                    client_order_id="at-class-capability-rebind",
                    capability=cap,
                    session=ready_session(),
                    at=NOW,
                    maximum_session_age_seconds=30,
                )
        finally:
            CapabilitySnapshot.admits = original

    def test_order_preparation_does_not_delegate_intent_revalidation_to_mutable_class_method(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        object.__setattr__(intent, "order_type", "FORGED")
        original = IbkrWebOrderIntent.__dict__["create"]
        IbkrWebOrderIntent.create = classmethod(
            lambda _cls, **_kwargs: intent
        )
        try:
            with self.assertRaisesRegex(
                IbkrWebAdapterError,
                "unsupported normalized order type",
            ):
                prepare_normalized_order(
                    intent,
                    client_order_id="at-intent-create-rebind",
                    capability=capability(),
                    session=ready_session(),
                    at=NOW,
                    maximum_session_age_seconds=30,
                )
        finally:
            IbkrWebOrderIntent.create = original

    def test_order_preparation_does_not_delegate_client_id_validation_to_mutable_module_lookup(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        original = ibkr_web_module.validate_coid
        ibkr_web_module.validate_coid = lambda _value: "at-forged-client-id"
        try:
            with self.assertRaisesRegex(IbkrWebAdapterError, "cOID is required"):
                prepare_normalized_order(
                    intent,
                    client_order_id="",
                    capability=capability(),
                    session=ready_session(),
                    at=NOW,
                    maximum_session_age_seconds=30,
                )
        finally:
            ibkr_web_module.validate_coid = original

    def test_order_preparation_keeps_bound_provider_order_type_mapping(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        original = ibkr_web_module._ORDER_TYPES
        ibkr_web_module._ORDER_TYPES = {"MARKET": "FORGED"}
        try:
            prepared = prepare_normalized_order(
                intent,
                client_order_id="at-order-map-rebind",
                capability=capability(),
                session=ready_session(),
                at=NOW,
                maximum_session_age_seconds=30,
            )
            self.assertEqual(prepared.fields["orderType"], "MKT")
        finally:
            ibkr_web_module._ORDER_TYPES = original

    def test_order_preparation_keeps_bound_decimal_text_formatter(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="LIMIT",
            time_in_force="DAY",
            quantity="1.25",
            limit_price="220.10",
        )
        original = ibkr_web_module._decimal_text
        ibkr_web_module._decimal_text = lambda _value: "0"
        try:
            prepared = prepare_normalized_order(
                intent,
                client_order_id="at-decimal-text-rebind",
                capability=capability(),
                session=ready_session(),
                at=NOW,
                maximum_session_age_seconds=30,
            )
            self.assertEqual(prepared.exact_quantity_text, "1.25")
            self.assertEqual(prepared.exact_limit_price_text, "220.10")
        finally:
            ibkr_web_module._decimal_text = original

    def test_order_preparation_uses_class_owned_capability_and_session_checks(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        cap = capability()
        session = ready_session()
        callbacks: list[str] = []

        def hostile_admits(**_kwargs):
            callbacks.append("capability")
            raise AssertionError("instance capability shadow executed")

        def hostile_ready():
            callbacks.append("session")
            raise AssertionError("instance session shadow executed")

        object.__setattr__(cap, "admits", hostile_admits)
        object.__setattr__(session, "require_trade_ready", hostile_ready)

        prepared = prepare_normalized_order(
            intent,
            client_order_id="at-instance-shadow-fence",
            capability=cap,
            session=session,
            at=NOW,
            maximum_session_age_seconds=30,
        )

        self.assertEqual(callbacks, [])
        self.assertEqual(prepared.fields["acctId"], "U1234567")

    def test_order_preparation_rejects_mutated_capability_text_before_callback(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        cap = capability()
        _HostileText.strip_called = False
        object.__setattr__(cap, "provider_id", _HostileText("IBKR"))

        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "capability.provider_id is required",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-mutated-capability-text",
                capability=cap,
                session=ready_session(),
                at=NOW,
                maximum_session_age_seconds=30,
            )
        self.assertFalse(_HostileText.strip_called)

    def test_order_preparation_rejects_mutated_session_fields_before_callbacks(self):
        class HostileTruth:
            called = False

            def __bool__(self):
                type(self).called = True
                raise AssertionError("hostile truth callback executed")

        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        session = ready_session()
        object.__setattr__(session, "connected", HostileTruth())

        with self.assertRaisesRegex(TypeError, "session connected.*exact boolean"):
            prepare_normalized_order(
                intent,
                client_order_id="at-mutated-session-bool",
                capability=capability(),
                session=session,
                at=NOW,
                maximum_session_age_seconds=30,
            )
        self.assertFalse(HostileTruth.called)

        hostile_time = _HostileDatetime(2026, 9, 24, 19, 59, tzinfo=timezone.utc)
        session = ready_session()
        object.__setattr__(session, "observed_at", hostile_time)
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "exact timezone-aware datetime",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-mutated-session-time",
                capability=capability(),
                session=session,
                at=NOW,
                maximum_session_age_seconds=30,
            )

    def test_order_preparation_reseals_mutated_intent_numeric_state(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        object.__setattr__(intent, "quantity", _HostileDecimal("1"))

        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "bounded exact decimal",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-mutated-intent-quantity",
                capability=capability(),
                session=ready_session(),
                at=NOW,
                maximum_session_age_seconds=30,
            )

    def test_order_preparation_reseals_mutated_contract_identity(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        _HostileInt.comparison_called = False
        object.__setattr__(intent.contract, "conid", _HostileInt(265598))

        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "positive exact integer",
        ):
            prepare_normalized_order(
                intent,
                client_order_id="at-mutated-contract",
                capability=capability(),
                session=ready_session(),
                at=NOW,
                maximum_session_age_seconds=30,
            )
        self.assertFalse(_HostileInt.comparison_called)

    def test_order_intent_rejects_contract_subclass_before_polymorphic_state(self):
        class ExecutableContract(IbkrContractIdentity):
            pass

        with self.assertRaisesRegex(TypeError, "exact IbkrContractIdentity"):
            IbkrWebOrderIntent.create(
                instrument_version="AAPL-CONID-265598:v1",
                account_id="U1234567",
                contract=object.__new__(ExecutableContract),
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity="1",
            )

    def test_normalized_limit_order_preserves_exact_decimal_outside_provider_double(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="LIMIT",
            time_in_force="DAY",
            quantity="1.25",
            limit_price="220.10",
        )
        prepared = prepare_normalized_order(
            intent,
            client_order_id="at-ibkr-1",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        self.assertEqual(prepared.endpoint, "/iserver/account/U1234567/orders")
        self.assertEqual(prepared.fields["conid"], 265598)
        self.assertEqual(prepared.fields["orderType"], "LMT")
        self.assertEqual(prepared.exact_quantity_text, "1.25")
        self.assertEqual(prepared.exact_limit_price_text, "220.10")
        self.assertFalse(prepared.provider_serialization_qualified)
        self.assertNotIn("quantity", prepared.fields)
        self.assertNotIn("price", prepared.fields)

    def test_crypto_like_routed_contract_uses_conidex(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conidex="557335679@ZEROHASH"),
            side="SELL",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="0.01",
        )
        prepared = prepare_normalized_order(
            intent,
            client_order_id="at-route-1",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        self.assertEqual(prepared.fields["conidex"], "557335679@ZEROHASH")
        self.assertNotIn("conid", prepared.fields)

    def test_provider_order_type_authority_map_is_immutable(self):
        with self.assertRaises(TypeError):
            ibkr_web_module._ORDER_TYPES["MARKET"] = "FORGED"
        self.assertEqual(ibkr_web_module._ORDER_TYPES["MARKET"], "MKT")

    def test_binary_float_quantity_is_rejected(self):
        with self.assertRaises(IbkrWebAdapterError):
            IbkrWebOrderIntent.create(
                instrument_version="AAPL-CONID-265598:v1",
                account_id="U1234567",
                contract=IbkrContractIdentity(conid=265598),
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity=1.0,
            )

    def test_future_session_observation_cannot_authorize(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "future"):
            prepare_normalized_order(
                intent,
                client_order_id="at-future-1",
                capability=capability(),
                session=ready_session(observed_at=NOW + timedelta(seconds=1)),
                at=NOW,
                maximum_session_age_seconds=30,
            )

    def test_stale_session_observation_cannot_authorize(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "session evidence is stale"):
            prepare_normalized_order(
                intent,
                client_order_id="at-stale-session",
                capability=capability(),
                session=ready_session(observed_at=NOW - timedelta(seconds=31)),
                at=NOW,
                maximum_session_age_seconds=30,
            )

    def test_session_freshness_policy_must_be_explicit_and_valid(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "non-negative integer"):
            prepare_normalized_order(
                intent,
                client_order_id="at-invalid-session-policy",
                capability=capability(),
                session=ready_session(),
                at=NOW,
                maximum_session_age_seconds=True,
            )


    def test_session_freshness_rejects_integer_subclass_before_comparison(self):
        _HostileInt.comparison_called = False
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "non-negative integer"):
            prepare_normalized_order(
                intent,
                client_order_id="at-hostile-session-age",
                capability=capability(),
                session=ready_session(),
                at=NOW,
                maximum_session_age_seconds=_HostileInt(30),
            )
        self.assertFalse(_HostileInt.comparison_called)

    def test_account_capability_must_match_exact_account(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "account"):
            prepare_normalized_order(
                intent,
                client_order_id="at-account-1",
                capability=capability(account_id="OTHER"),
                session=ready_session(),
                at=NOW,
                maximum_session_age_seconds=30,
            )

    def test_execution_identity_uses_exec_id_and_perm_id(self):
        execution = IbkrExecutionEvidence.create(
            execution_id="0001.123.01",
            permanent_order_id=778899,
            account_id="U1234567",
            quantity="0.5",
            price="220.10",
        )
        self.assertEqual(execution.execution_id, "0001.123.01")
        self.assertEqual(execution.permanent_order_id, "778899")
        self.assertEqual(execution.quantity, Decimal("0.5"))

    def test_execution_permanent_order_id_must_be_non_negative_integer(self):
        for invalid in (None, True, -1, "778899", _HostileInt(778899)):
            with self.subTest(invalid=invalid):
                with self.assertRaises(IbkrWebAdapterError):
                    IbkrExecutionEvidence.create(
                        execution_id="0001.123.01",
                        permanent_order_id=invalid,
                        account_id="U1234567",
                        quantity="0.5",
                        price="220.10",
                    )

    def test_incomplete_execution_surfaces_do_not_prove_absence(self):
        evidence = IbkrAbsenceEvidence(
            exact_client_order_lookup_complete=True,
            exact_client_order_absent=True,
            open_orders_complete=True,
            completed_orders_complete=True,
            executions_complete=False,
            account_activity_complete=True,
            consistency_horizon_satisfied=True,
            exclusion_semantics_qualified=False,
            order_found=False,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

    def test_foundation_cannot_self_assert_exclusion_semantics_qualification(self):
        with self.assertRaisesRegex(
            IbkrWebAdapterError,
            "cannot self-assert exclusion semantics qualification",
        ):
            IbkrAbsenceEvidence(
                exact_client_order_lookup_complete=True,
                exact_client_order_absent=True,
                open_orders_complete=True,
                completed_orders_complete=True,
                executions_complete=True,
                account_activity_complete=True,
                consistency_horizon_satisfied=True,
                exclusion_semantics_qualified=True,
                order_found=False,
            )


    def test_complete_generic_surfaces_without_exact_lookup_are_still_inconclusive(self):
        evidence = IbkrAbsenceEvidence(
            exact_client_order_lookup_complete=False,
            exact_client_order_absent=False,
            open_orders_complete=True,
            completed_orders_complete=True,
            executions_complete=True,
            account_activity_complete=True,
            consistency_horizon_satisfied=True,
            exclusion_semantics_qualified=False,
            order_found=False,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

    def test_absence_requires_qualified_exclusion_semantics(self):
        evidence = IbkrAbsenceEvidence(
            exact_client_order_lookup_complete=True,
            exact_client_order_absent=True,
            open_orders_complete=True,
            completed_orders_complete=True,
            executions_complete=True,
            account_activity_complete=True,
            consistency_horizon_satisfied=True,
            exclusion_semantics_qualified=False,
            order_found=False,
        )
        self.assertEqual(evidence.verdict(), "INCONCLUSIVE")

    def test_contradictory_exact_absence_and_found_order_is_rejected(self):
        with self.assertRaisesRegex(IbkrWebAdapterError, "conflicts"):
            IbkrAbsenceEvidence(
                exact_client_order_lookup_complete=True,
                exact_client_order_absent=True,
                open_orders_complete=True,
                completed_orders_complete=True,
                executions_complete=True,
                account_activity_complete=True,
                consistency_horizon_satisfied=True,
                exclusion_semantics_qualified=False,
                order_found=True,
            )

    def test_cancel_request_is_single_ticket_and_dispatch_neutral(self):
        request = prepare_cancel_request(
            account_id="U1234567",
            provider_order_id="123456789",
            regulatory_manual_indicator_required=False,
        )
        self.assertIsInstance(request, IbkrCancelRequest)
        self.assertEqual(
            request.endpoint,
            "/iserver/account/U1234567/order/123456789",
        )
        self.assertEqual(dict(request.query), {})

        for unsafe_order_id in ("-1", "0", "01", " 123", "123 ", "", None, 123):
            with self.subTest(provider_order_id=unsafe_order_id):
                with self.assertRaises((IbkrWebAdapterError, TypeError)):
                    prepare_cancel_request(
                        account_id="U1234567",
                        provider_order_id=unsafe_order_id,
                        regulatory_manual_indicator_required=False,
                    )

        for unsafe_account in (
            " U1234567",
            "U1234567 ",
            "../U1234567",
            "U123/4567",
            "",
        ):
            with self.subTest(account_id=unsafe_account):
                with self.assertRaises(IbkrWebAdapterError):
                    prepare_cancel_request(
                        account_id=unsafe_account,
                        provider_order_id="123456789",
                        regulatory_manual_indicator_required=False,
                    )

    def test_regulated_cancel_requires_exact_manual_metadata(self):
        request = prepare_cancel_request(
            account_id="U1234567",
            provider_order_id="123456789",
            regulatory_manual_indicator_required=True,
            manual_indicator=False,
            ext_operator="operator-1",
        )
        self.assertEqual(
            dict(request.query),
            {"manualIndicator": "false", "extOperator": "operator-1"},
        )

        for manual_indicator, ext_operator in (
            (None, "operator-1"),
            (False, None),
            (1, "operator-1"),
            (False, " operator-1"),
        ):
            with self.subTest(
                manual_indicator=manual_indicator,
                ext_operator=ext_operator,
            ):
                with self.assertRaises((IbkrWebAdapterError, TypeError)):
                    prepare_cancel_request(
                        account_id="U1234567",
                        provider_order_id="123456789",
                        regulatory_manual_indicator_required=True,
                        manual_indicator=manual_indicator,
                        ext_operator=ext_operator,
                    )

        with self.assertRaisesRegex(
            IbkrWebAdapterError, "unqualified cancel metadata"
        ):
            prepare_cancel_request(
                account_id="U1234567",
                provider_order_id="123456789",
                regulatory_manual_indicator_required=False,
                manual_indicator=False,
                ext_operator="operator-1",
            )

    def test_cancel_request_direct_constructor_cannot_widen_scope(self):
        base = {
            "endpoint": "/iserver/account/U1234567/order/123456789",
            "query": {},
            "account_id": "U1234567",
            "provider_order_id": "123456789",
        }
        with self.assertRaisesRegex(IbkrWebAdapterError, "positive integer"):
            IbkrCancelRequest(**{**base, "provider_order_id": "-1"})
        with self.assertRaisesRegex(IbkrWebAdapterError, "endpoint"):
            IbkrCancelRequest(
                **{
                    **base,
                    "endpoint": "/iserver/account/U1234567/order/-1",
                }
            )
        with self.assertRaisesRegex(IbkrWebAdapterError, "unsupported"):
            IbkrCancelRequest(**{**base, "query": {"all": "true"}})
        with self.assertRaisesRegex(IbkrWebAdapterError, "together"):
            IbkrCancelRequest(
                **{**base, "query": {"manualIndicator": "false"}}
            )
        with self.assertRaisesRegex(TypeError, "exact dict"):
            IbkrCancelRequest(
                **{**base, "query": MappingProxyType({})}
            )

    def test_cancel_request_rejects_hostile_key_before_hash_callback(self):
        class HostileCancelKey(str):
            armed = False

            def __hash__(self):
                if type(self).armed:
                    raise AssertionError("hostile cancel-key hash executed")
                return str.__hash__(self)

        key = HostileCancelKey("manualIndicator")
        query = {key: "false", "extOperator": "operator-1"}
        HostileCancelKey.armed = True
        try:
            with self.assertRaisesRegex(
                IbkrWebAdapterError,
                "cancel query must use exact text",
            ):
                IbkrCancelRequest(
                    endpoint="/iserver/account/U1234567/order/123456789",
                    query=query,
                    account_id="U1234567",
                    provider_order_id="123456789",
                )
        finally:
            HostileCancelKey.armed = False

    def test_cancel_acknowledgement_never_proves_terminal_cancel(self):
        outcome = parse_cancel_response(
            provider_order_id="123456789",
            payload={
                "msg": "Request was submitted",
                "order_id": 123456789,
                "conid": 265598,
                "account": "U1234567",
            },
        )
        self.assertTrue(outcome.acknowledged)
        self.assertFalse(outcome.terminal_cancel_proven)
        self.assertEqual(outcome.provider_order_id, "123456789")

    def test_cancel_provider_error_is_not_terminal_cancel(self):
        outcome = parse_cancel_response(
            provider_order_id="123456789",
            payload={"error": "Order cannot be cancelled"},
        )
        self.assertFalse(outcome.acknowledged)
        self.assertFalse(outcome.terminal_cancel_proven)
        self.assertEqual(outcome.message, "Order cannot be cancelled")

        with self.assertRaisesRegex(IbkrWebAdapterError, "ambiguous"):
            parse_cancel_response(
                provider_order_id="123456789",
                payload={
                    "error": "Order cannot be cancelled",
                    "order_id": 123456789,
                    "msg": "Request was submitted",
                },
            )

    def test_acknowledgement_is_not_fill_or_retry_permission(self):
        outcome = parse_order_submission_response(
            [
                {
                    "order_id": "1234567890",
                    "order_status": "Submitted",
                    "encrypt_message": "1",
                }
            ]
        )
        self.assertEqual(outcome.status, "ACKNOWLEDGED")
        self.assertEqual(outcome.provider_order_id, "1234567890")
        self.assertFalse(outcome.proves_fill)
        self.assertFalse(outcome.retry_same_economic_action)

    def test_unrecorded_reply_message_cannot_prepare_second_request(self):
        outcome = parse_order_submission_response(
            [
                {
                    "id": "07a13a5a-4a48-44a5-bb25-5ab37b79186c",
                    "message": ["Order exceeds configured price constraint."],
                    "isSuppressed": False,
                    "messageIds": ["o163"],
                }
            ]
        )
        self.assertEqual(outcome.status, "REPLY_REQUIRED")
        self.assertFalse(outcome.proves_fill)
        with self.assertRaisesRegex(TypeError, "recorded"):
            prepare_reply_confirmation(
                outcome,
                expected_attempt_id="attempt-1",
                expected_account_id="U1234567",
                expected_client_order_id="coid-1",
                explicit_authorization=True,
            )

    def test_reply_identity_cannot_escape_reply_endpoint_or_mutate_confirmation_body(self):
        with self.assertRaisesRegex(IbkrWebAdapterError, "path segment"):
            parse_order_submission_response(
                [{"id": "../orders", "message": ["Confirm"], "messageIds": ["o1"]}]
            )
        with self.assertRaisesRegex(IbkrWebAdapterError, "path segment"):
            parse_order_submission_response(
                [{"id": "reply?confirmed=false", "message": ["Confirm"], "messageIds": ["o1"]}]
            )
        for unsafe_id in (".", ".."):
            with self.subTest(unsafe_id=unsafe_id), self.assertRaisesRegex(
                IbkrWebAdapterError, "path segment"
            ):
                parse_order_submission_response(
                    [{"id": unsafe_id, "message": ["Confirm"], "messageIds": ["o1"]}]
                )
        for invalid_reply_id in (None, True, 123):
            with self.subTest(reply_id=invalid_reply_id), self.assertRaisesRegex(
                IbkrWebAdapterError,
                "reply id must be a string",
            ):
                parse_order_submission_response(
                    [{"id": invalid_reply_id, "message": ["Confirm"], "messageIds": ["o1"]}]
                )

        base = {
            "endpoint": "/iserver/reply/safe-reply-id",
            "body": {"confirmed": True},
            "attempt_id": "attempt-1",
            "account_id": "U1234567",
            "client_order_id": "at-reply-direct",
            "response_sha256": "sha256:" + "a" * 64,
        }
        request = IbkrReplyRequest(**base)
        self.assertEqual(dict(request.body), {"confirmed": True})
        with self.assertRaisesRegex(IbkrWebAdapterError, "reply endpoint|path segment"):
            IbkrReplyRequest(**{**base, "endpoint": "/iserver/reply/../orders"})
        with self.assertRaisesRegex(IbkrWebAdapterError, "path segment"):
            IbkrReplyRequest(**{**base, "endpoint": "/iserver/reply/.."})
        with self.assertRaisesRegex(IbkrWebAdapterError, "confirmed=true"):
            IbkrReplyRequest(**{**base, "body": {"confirmed": False}})

    def test_ambiguous_ack_and_reply_shape_fails_closed(self):
        with self.assertRaisesRegex(IbkrWebAdapterError, "ambiguous"):
            parse_order_submission_response(
                [
                    {
                        "order_id": "123",
                        "order_status": "Submitted",
                        "id": "reply-1",
                        "message": ["Confirm"],
                    }
                ]
            )

    def test_explicit_provider_error_is_rejected_but_not_retryable(self):
        outcome = parse_order_submission_response([{"error": "order rejected"}])
        self.assertEqual(outcome.status, "REJECTED")
        self.assertEqual(outcome.rejection_reason, "order rejected")
        self.assertFalse(outcome.retry_same_economic_action)

    def test_recorded_ack_is_bound_to_attempt_and_never_fill(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        normalized = prepare_normalized_order(
            intent,
            client_order_id="at-ibkr-submit-1",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        recorded = record_order_submission_result(
            normalized,
            attempt_id="attempt-ibkr-1",
            account_id="U1234567",
            environment="PAPER",
            observed_at=NOW,
            response_body=(
                '[{"order_id":"1234567890","order_status":"Submitted",'
                '"encrypt_message":"1"}]'
            ),
        )
        self.assertEqual(recorded.outcome, "ACKNOWLEDGED")
        self.assertEqual(recorded.next_action, "OBSERVE_OR_RECONCILE")
        self.assertEqual(recorded.client_order_id, "at-ibkr-submit-1")
        self.assertEqual(recorded.provider_order_id, "1234567890")
        self.assertTrue(recorded.response_sha256.startswith("sha256:"))
        self.assertFalse(recorded.proves_fill)
        self.assertFalse(recorded.retry_same_economic_action)

    def test_ambiguous_ibkr_transport_is_unknown_and_reconcile_first(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        normalized = prepare_normalized_order(
            intent,
            client_order_id="at-ibkr-unknown",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        recorded = record_order_submission_result(
            normalized,
            attempt_id="attempt-ibkr-unknown",
            account_id="U1234567",
            environment="PAPER",
            observed_at=NOW,
            response_body=None,
            transport_ambiguous=True,
        )
        self.assertEqual(recorded.outcome, "UNKNOWN")
        self.assertEqual(recorded.next_action, "RECONCILE_FIRST")
        self.assertIsNone(recorded.response_sha256)
        self.assertIsNone(recorded.provider_order_id)
        self.assertFalse(recorded.retry_same_economic_action)

    def test_recorded_reply_requires_explicit_second_guarded_action(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="LIMIT",
            time_in_force="DAY",
            quantity="1",
            limit_price="220.10",
        )
        normalized = prepare_normalized_order(
            intent,
            client_order_id="at-ibkr-reply",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        recorded = record_order_submission_result(
            normalized,
            attempt_id="attempt-ibkr-reply",
            account_id="U1234567",
            environment="PAPER",
            observed_at=NOW,
            response_body=(
                '[{"id":"07a13a5a-4a48-44a5-bb25-5ab37b79186c",'
                '"message":["Confirm this order"],"isSuppressed":false,'
                '"messageIds":["o163"]}]'
            ),
        )
        self.assertEqual(recorded.outcome, "REPLY_REQUIRED")
        self.assertEqual(recorded.next_action, "EXPLICIT_REPLY_REQUIRED")
        self.assertEqual(
            recorded.reply_id,
            "07a13a5a-4a48-44a5-bb25-5ab37b79186c",
        )
        self.assertFalse(recorded.retry_same_economic_action)

        with self.assertRaisesRegex(IbkrWebAdapterError, "explicit authorization"):
            prepare_reply_confirmation(
                recorded,
                expected_attempt_id="attempt-ibkr-reply",
                expected_account_id="U1234567",
                expected_client_order_id="at-ibkr-reply",
                explicit_authorization=False,
            )

        request = prepare_reply_confirmation(
            recorded,
            expected_attempt_id="attempt-ibkr-reply",
            expected_account_id="U1234567",
            expected_client_order_id="at-ibkr-reply",
            explicit_authorization=True,
        )
        self.assertEqual(
            request.endpoint,
            "/iserver/reply/07a13a5a-4a48-44a5-bb25-5ab37b79186c",
        )
        self.assertEqual(dict(request.body), {"confirmed": True})
        self.assertEqual(request.attempt_id, "attempt-ibkr-reply")
        self.assertEqual(request.account_id, "U1234567")
        self.assertEqual(request.client_order_id, "at-ibkr-reply")
        self.assertEqual(request.response_sha256, recorded.response_sha256)

        for field, value in (
            ("expected_attempt_id", "other-attempt"),
            ("expected_account_id", "OTHER"),
            ("expected_client_order_id", "other-coid"),
        ):
            kwargs = {
                "expected_attempt_id": "attempt-ibkr-reply",
                "expected_account_id": "U1234567",
                "expected_client_order_id": "at-ibkr-reply",
                "explicit_authorization": True,
            }
            kwargs[field] = value
            with self.subTest(field=field), self.assertRaises(IbkrWebAdapterError):
                prepare_reply_confirmation(recorded, **kwargs)

    def test_recorded_submission_rejects_cross_account_binding(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
        )
        normalized = prepare_normalized_order(
            intent,
            client_order_id="at-ibkr-account",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "account"):
            record_order_submission_result(
                normalized,
                attempt_id="attempt-cross-account",
                account_id="OTHER",
                environment="PAPER",
                observed_at=NOW,
                response_body=(
                    '[{"order_id":"123","order_status":"Submitted",'
                    '"encrypt_message":"1"}]'
                ),
            )

    def test_unique_execution_maps_to_canonical_reconciliation_fill(self):
        execution = IbkrExecutionEvidence.create(
            execution_id="0001.123.01",
            permanent_order_id=778899,
            account_id="U1234567",
            quantity="0.5",
            price="220.10",
        )
        fill = execution_to_reconciliation_fill(
            execution,
            client_order_id="at-ibkr-1",
            expected_account_id="U1234567",
            instrument="AAPL-CONID-265598:v1",
            fee_amount="-0.35",
            fee_currency="USD",
            trade_time="2026-09-24T20:00:01Z",
            evidence_refs=EXECUTION_EVIDENCE_REFS,
        )
        self.assertEqual(fill.provider_id, "IBKR")
        self.assertEqual(fill.account_id, "U1234567")
        self.assertEqual(fill.environment, "PAPER")
        self.assertEqual(fill.provider_execution_id, "0001.123.01")
        self.assertEqual(fill.client_order_id, "at-ibkr-1")
        self.assertEqual(fill.quantity, Decimal("0.5"))
        self.assertEqual(fill.price, Decimal("220.10"))
        self.assertEqual(fill.fee_amount, Decimal("-0.35"))
        self.assertEqual(fill.fee_currency, "USD")
        self.assertEqual(fill.evidence_refs, EXECUTION_EVIDENCE_REFS)



    def test_tws_execution_reconciliation_refuses_missing_or_noncanonical_provenance(self):
        execution = IbkrExecutionEvidence.create(
            execution_id="0001.provenance.01",
            permanent_order_id=778899,
            account_id="U1234567",
            quantity="1",
            price="100",
        )
        for refs in (
            (),
            ("",),
            (" bad-ref",),
            ("duplicate", "duplicate"),
            ["not-a-tuple"],
        ):
            with self.subTest(evidence_refs=refs):
                with self.assertRaisesRegex(
                    IbkrWebAdapterError, "evidence_refs"
                ):
                    execution_to_reconciliation_fill(
                        execution,
                        environment="PAPER",
                        client_order_id="at-ibkr-provenance",
                        expected_account_id="U1234567",
                        instrument="AAPL-CONID-265598:v1",
                        fee_amount="0",
                        fee_currency="USD",
                        trade_time="2026-09-24T20:00:01Z",
                        evidence_refs=refs,
                    )

    def test_tws_execution_correction_maps_to_stable_reconciliation_identity(self):
        correction = IbkrExecutionEvidence.create(
            execution_id="0000e0d5.6576fd38.01.02",
            permanent_order_id=778899,
            account_id="U1234567",
            quantity="2",
            price="99",
        )
        fill = execution_to_reconciliation_fill(
            correction,
            environment="PAPER",
            client_order_id="at-ibkr-correction",
            expected_account_id="U1234567",
            instrument="AAPL-CONID-265598:v1",
            fee_amount="0.30",
            fee_currency="USD",
            trade_time="2026-09-24T20:00:01Z",
            evidence_refs=EXECUTION_EVIDENCE_REFS,
        )
        self.assertEqual(
            fill.provider_execution_id,
            "0000e0d5.6576fd38.01.01",
        )

    def test_execution_evidence_accepts_zero_perm_id_for_external_activity(self):
        execution = IbkrExecutionEvidence.create(
            execution_id="external.1.01",
            permanent_order_id=0,
            account_id="U1234567",
            quantity="1",
            price="100",
        )
        self.assertEqual(execution.permanent_order_id, "0")
        fill = execution_to_reconciliation_fill(
            execution,
            client_order_id=None,
            expected_account_id="U1234567",
            instrument="AAPL-CONID-265598:v1",
            fee_amount="0",
            fee_currency="USD",
            trade_time="2026-09-24T20:00:01Z",
            evidence_refs=EXECUTION_EVIDENCE_REFS,
        )
        self.assertEqual(fill.provider_execution_id, "external.1.01")

        with self.assertRaisesRegex(
            IbkrWebAdapterError, "non-negative exact integer"
        ):
            IbkrExecutionEvidence.create(
                execution_id="external.bad.01",
                permanent_order_id=-1,
                account_id="U1234567",
                quantity="1",
                price="100",
            )
        with self.assertRaisesRegex(
            IbkrWebAdapterError, "canonical non-negative integer text"
        ):
            IbkrExecutionEvidence(
                execution_id="external.bad.02",
                permanent_order_id="00",
                account_id="U1234567",
                quantity=Decimal("1"),
                price=Decimal("100"),
            )

    def test_web_api_trades_require_activity_surface_provenance(self):
        binding = prepare_authenticated_read_query(
            capability=capability(),
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/iserver/account/trades",
            query={},
            at=NOW,
        )
        observation = observe_authenticated_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=b"[]",
            observed_at=NOW,
        )
        with self.assertRaisesRegex(ProviderCoreError, "surface mismatch"):
            parse_web_api_trades(
                observation,
                instrument_versions_by_conid={},
                fee_currency_by_execution_id={},
            )

    def test_web_api_trades_ignore_observation_instance_scope_shadow(self):
        observation = ibkr_trade_observation([])
        callbacks: list[str] = []

        def hostile_scope(**_kwargs):
            callbacks.append("scope")
            raise AssertionError("observation instance scope callback executed")

        object.__setattr__(observation, "require_scope", hostile_scope)
        fills = parse_web_api_trades(
            observation,
            instrument_versions_by_conid={},
            fee_currency_by_execution_id={},
        )
        self.assertEqual(fills, ())
        self.assertEqual(callbacks, [])

    def test_web_api_trades_reject_mutated_binding_text_before_callback(self):
        observation = ibkr_trade_observation([])
        _HostileText.strip_called = False
        object.__setattr__(
            observation.query_binding,
            "provider_id",
            _HostileText("IBKR"),
        )

        with self.assertRaisesRegex(
            ProviderCoreError,
            "authenticated-read binding changed after preparation",
        ):
            parse_web_api_trades(
                observation,
                instrument_versions_by_conid={},
                fee_currency_by_execution_id={},
            )
        self.assertFalse(_HostileText.strip_called)

    def test_web_api_trades_reject_mutated_evidence_ref_before_use(self):
        observation = ibkr_trade_observation([])
        _HostileText.strip_called = False
        object.__setattr__(
            observation,
            "evidence_ref",
            _HostileText("provider-read:sha256:" + "1" * 64),
        )

        with self.assertRaisesRegex(
            ProviderCoreError,
            "provider response changed after exact-byte observation",
        ):
            parse_web_api_trades(
                observation,
                instrument_versions_by_conid={},
                fee_currency_by_execution_id={},
            )
        self.assertFalse(_HostileText.strip_called)

    def test_web_api_trades_use_execution_identity_coid_and_explicit_fee_currency(self):
        rows = [
            {
                "execution_id": "0001.123.01",
                "order_ref": "at-ibkr-1",
                "account": "U1234567",
                "side": "B",
                "conid": 265598,
                "size": Decimal("0.5"),
                "price": "220.10",
                "commission": "-0.35",
                "trade_time": "20260924-20:00:01",
                "trade_time_r": 1790280001000,
            }
        ]
        observation = ibkr_trade_observation([rows[0], dict(rows[0])])
        fills = parse_web_api_trades(
            observation,
            instrument_versions_by_conid={265598: "AAPL-CONID-265598:v1"},
            fee_currency_by_execution_id={"0001.123.01": "USD"},
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].provider_id, "IBKR")
        self.assertEqual(fills[0].account_id, "U1234567")
        self.assertEqual(fills[0].environment, "PAPER")
        self.assertEqual(fills[0].provider_execution_id, "0001.123.01")
        self.assertEqual(fills[0].client_order_id, "at-ibkr-1")
        self.assertEqual(fills[0].side, "BUY")
        self.assertEqual(fills[0].quantity, Decimal("0.5"))
        self.assertEqual(fills[0].fee_amount, Decimal("-0.35"))
        self.assertEqual(fills[0].trade_time, "2026-09-24T20:00:01Z")
        self.assertEqual(fills[0].evidence_refs, (observation.evidence_ref,))

    def test_web_api_trade_time_and_conidex_are_cross_bound(self):
        base = {
            "execution_id": "exec-time-1",
            "order_ref": "at-ibkr-time",
            "account": "U1234567",
            "accountCode": "U1234567",
            "side": "B",
            "conid": 265598,
            "conidEx": "265598@SMART",
            "size": "1",
            "price": "100",
            "commission": "0.25",
            "trade_time": "20260924-20:00:01",
            "trade_time_r": 1790280001000,
        }
        fills = parse_web_api_trades(
            ibkr_trade_observation([base]),
            instrument_versions_by_conid={265598: "AAPL:v1"},
            fee_currency_by_execution_id={"exec-time-1": "USD"},
        )
        self.assertEqual(fills[0].trade_time, "2026-09-24T20:00:01Z")

        manual = parse_web_api_trades(
            ibkr_trade_observation([dict(base, order_ref="")]),
            instrument_versions_by_conid={265598: "AAPL:v1"},
            fee_currency_by_execution_id={"exec-time-1": "USD"},
        )
        self.assertIsNone(manual[0].client_order_id)
        with self.assertRaisesRegex(IbkrWebAdapterError, "canonical provider text"):
            parse_web_api_trades(
                ibkr_trade_observation([dict(base, order_ref=" at-ibkr-time")]),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-time-1": "USD"},
            )

        with self.assertRaisesRegex(
            IbkrWebAdapterError, "combo/spread trade reconciliation is not qualified"
        ):
            parse_web_api_trades(
                ibkr_trade_observation(
                    [dict(base, conidEx="265598;;;43645865/1,9408/-1")]
                ),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-time-1": "USD"},
            )

        with self.assertRaisesRegex(IbkrWebAdapterError, "trade_time_r conflict"):
            parse_web_api_trades(
                ibkr_trade_observation([dict(base, trade_time_r=1790280002000)]),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-time-1": "USD"},
            )
        with self.assertRaisesRegex(IbkrWebAdapterError, "documented IBKR UTC format"):
            parse_web_api_trades(
                ibkr_trade_observation(
                    [dict(base, trade_time="2026-09-24T20:00:01Z")]
                ),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-time-1": "USD"},
            )
        missing_epoch = dict(base)
        missing_epoch.pop("trade_time_r")
        with self.assertRaisesRegex(IbkrWebAdapterError, "trade_time_r"):
            parse_web_api_trades(
                ibkr_trade_observation([missing_epoch]),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-time-1": "USD"},
            )
        with self.assertRaisesRegex(IbkrWebAdapterError, "conidEx"):
            parse_web_api_trades(
                ibkr_trade_observation(
                    [dict(base, conidEx="999999@SMART")]
                ),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-time-1": "USD"},
            )

    def test_web_api_trade_rejects_cross_account_unknown_conid_and_missing_fee_currency(self):
        row = {
            "execution_id": "exec-1",
            "order_ref": "at-ibkr-1",
            "account": "U1234567",
            "side": "B",
            "conid": 265598,
            "size": "1",
            "price": "100",
            "commission": "0.25",
            "trade_time": "20260924-20:00:01",
            "trade_time_r": 1790280001000,
        }
        with self.assertRaisesRegex(IbkrWebAdapterError, "account"):
            parse_web_api_trades(
                ibkr_trade_observation([row], account_id="OTHER"),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-1": "USD"},
            )
        with self.assertRaisesRegex(IbkrWebAdapterError, "unmapped IBKR conid"):
            parse_web_api_trades(
                ibkr_trade_observation([row]),
                instrument_versions_by_conid={},
                fee_currency_by_execution_id={"exec-1": "USD"},
            )
        with self.assertRaisesRegex(IbkrWebAdapterError, "fee currency"):
            parse_web_api_trades(
                ibkr_trade_observation([row]),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={},
            )

    def test_web_api_trade_rejects_conflicting_account_aliases(self):
        row = {
            "execution_id": "exec-account-alias",
            "order_ref": "at-ibkr-account-alias",
            "account": "U1234567",
            "accountCode": "OTHER",
            "side": "B",
            "conid": 265598,
            "size": "1",
            "price": "100",
            "commission": "0.25",
            "trade_time": "20260924-20:00:01",
            "trade_time_r": 1790280001000,
        }
        with self.assertRaisesRegex(IbkrWebAdapterError, "identifiers conflict"):
            parse_web_api_trades(
                ibkr_trade_observation([row]),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-account-alias": "USD"},
            )

    def test_web_api_trade_lookup_authorities_require_inert_exact_content(self):
        row = {
            "execution_id": "exec-lookup",
            "order_ref": "at-ibkr-lookup",
            "account": "U1234567",
            "accountCode": "U1234567",
            "side": "B",
            "conid": 265598,
            "size": "1",
            "price": "100",
            "commission": "0.25",
            "trade_time": "20260924-20:00:01",
            "trade_time_r": 1790280001000,
        }
        observation = ibkr_trade_observation([row])
        invalid_cases = (
            (
                {True: "AAPL:v1"},
                {"exec-lookup": "USD"},
                "positive exact integers",
            ),
            (
                {265598: 123},
                {"exec-lookup": "USD"},
                "values must be exact text",
            ),
            (
                {265598: "AAPL:v1"},
                {1: "USD"},
                "keys must be exact text",
            ),
            (
                {265598: "AAPL:v1"},
                {"exec-lookup": 123},
                "values must be exact text",
            ),
            (
                {265598: " AAPL:v1"},
                {"exec-lookup": "USD"},
                "values must be exact text",
            ),
            (
                {265598: "AAPL:v1"},
                {" exec-lookup": "USD"},
                "keys must be exact text",
            ),
            (
                {265598: "AAPL:v1"},
                {"exec-lookup": " USD"},
                "values must be exact text",
            ),
        )
        for instruments, currencies, message in invalid_cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                IbkrWebAdapterError, message
            ):
                parse_web_api_trades(
                    observation,
                    instrument_versions_by_conid=instruments,
                    fee_currency_by_execution_id=currencies,
                )

    def test_web_api_trade_preserves_exact_json_number_economics_and_conflicting_execution_id(self):
        base = {
            "execution_id": "exec-1",
            "order_ref": "at-ibkr-1",
            "account": "U1234567",
            "side": "B",
            "conid": 265598,
            "size": "1",
            "price": "100",
            "commission": "0.25",
            "trade_time": "20260924-20:00:01",
            "trade_time_r": 1790280001000,
        }
        fills = parse_web_api_trades(
            ibkr_trade_observation([dict(base, size=1.0)]),
            instrument_versions_by_conid={265598: "AAPL:v1"},
            fee_currency_by_execution_id={"exec-1": "USD"},
        )
        self.assertEqual(fills[0].quantity, Decimal("1.0"))
        with self.assertRaisesRegex(IbkrWebAdapterError, "conflicting"):
            parse_web_api_trades(
                ibkr_trade_observation([base, dict(base, size="2")]),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-1": "USD"},
            )

    def test_web_api_trade_correction_supersedes_prior_revision(self):
        base = {
            "execution_id": "0000e0d5.6576fd38.01.01",
            "order_ref": "at-ibkr-correction",
            "account": "U1234567",
            "accountCode": "U1234567",
            "side": "B",
            "conid": 265598,
            "size": "1",
            "price": "100",
            "commission": "0.25",
            "trade_time": "20260924-20:00:01",
            "trade_time_r": 1790280001000,
        }
        corrected = dict(
            base,
            execution_id="0000e0d5.6576fd38.01.02",
            size="2",
            price="99",
            commission="0.30",
        )
        fees = {
            "0000e0d5.6576fd38.01.01": "USD",
            "0000e0d5.6576fd38.01.02": "USD",
        }
        for rows in ([base, corrected], [corrected, base]):
            with self.subTest(order=[row["execution_id"] for row in rows]):
                fills = parse_web_api_trades(
                    ibkr_trade_observation(rows),
                    instrument_versions_by_conid={265598: "AAPL:v1"},
                    fee_currency_by_execution_id=fees,
                )
                self.assertEqual(len(fills), 1)
                self.assertEqual(
                    fills[0].provider_execution_id,
                    "0000e0d5.6576fd38.01.01",
                )
                self.assertEqual(fills[0].quantity, Decimal("2"))
                self.assertEqual(fills[0].price, Decimal("99"))
                self.assertEqual(fills[0].fee_amount, Decimal("0.30"))

        other = dict(
            base,
            execution_id="0000e0d5.6576fd38.02.01",
            order_ref="at-ibkr-other",
            trade_time="20260924-19:59:59",
            trade_time_r=1790279999000,
        )
        three_fees = {**fees, "0000e0d5.6576fd38.02.01": "USD"}
        forward = parse_web_api_trades(
            ibkr_trade_observation([corrected, other]),
            instrument_versions_by_conid={265598: "AAPL:v1"},
            fee_currency_by_execution_id=three_fees,
        )
        reverse = parse_web_api_trades(
            ibkr_trade_observation([other, corrected]),
            instrument_versions_by_conid={265598: "AAPL:v1"},
            fee_currency_by_execution_id=three_fees,
        )
        self.assertEqual(
            tuple(fill.provider_execution_id for fill in forward),
            tuple(fill.provider_execution_id for fill in reverse),
        )
        self.assertEqual(forward[0].provider_execution_id, "0000e0d5.6576fd38.02.01")

        initial_only = parse_web_api_trades(
            ibkr_trade_observation([base]),
            instrument_versions_by_conid={265598: "AAPL:v1"},
            fee_currency_by_execution_id=fees,
        )
        correction_only = parse_web_api_trades(
            ibkr_trade_observation([corrected]),
            instrument_versions_by_conid={265598: "AAPL:v1"},
            fee_currency_by_execution_id=fees,
        )
        self.assertEqual(
            initial_only[0].provider_execution_id,
            correction_only[0].provider_execution_id,
        )
        self.assertEqual(
            correction_only[0].provider_execution_id,
            "0000e0d5.6576fd38.01.01",
        )

        independent_partial = dict(
            base,
            execution_id="0000e0d5.6576fd38.03.01",
            order_ref="at-ibkr-partial-3",
        )
        independent = parse_web_api_trades(
            ibkr_trade_observation([base, independent_partial]),
            instrument_versions_by_conid={265598: "AAPL:v1"},
            fee_currency_by_execution_id={
                **fees,
                "0000e0d5.6576fd38.03.01": "USD",
            },
        )
        self.assertEqual(
            {fill.provider_execution_id for fill in independent},
            {
                "0000e0d5.6576fd38.01.01",
                "0000e0d5.6576fd38.03.01",
            },
        )

        with self.assertRaisesRegex(
            IbkrWebAdapterError, "revision must be a positive integer"
        ):
            parse_web_api_trades(
                ibkr_trade_observation(
                    [dict(base, execution_id="0000e0d5.6576fd38.01.00")]
                ),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={
                    **fees,
                    "0000e0d5.6576fd38.01.00": "USD",
                },
            )

        ambiguous = dict(
            corrected,
            execution_id="0000e0d5.6576fd38.01.2",
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "revision is ambiguous"):
            parse_web_api_trades(
                ibkr_trade_observation([corrected, ambiguous]),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={
                    **fees,
                    "0000e0d5.6576fd38.01.2": "USD",
                },
            )

    def test_web_api_trade_side_is_provider_evidenced_and_fail_closed(self):
        base = {
            "execution_id": "exec-side-1",
            "order_ref": "at-ibkr-side",
            "account": "U1234567",
            "side": "S",
            "conid": 265598,
            "size": "1",
            "price": "100",
            "commission": "0.25",
            "trade_time": "20260924-20:00:01",
            "trade_time_r": 1790280001000,
        }
        fills = parse_web_api_trades(
            ibkr_trade_observation([base]),
            instrument_versions_by_conid={265598: "AAPL:v1"},
            fee_currency_by_execution_id={"exec-side-1": "USD"},
        )
        self.assertEqual(fills[0].side, "SELL")

        missing = dict(base)
        missing.pop("side")
        with self.assertRaisesRegex(IbkrWebAdapterError, "trade.side"):
            parse_web_api_trades(
                ibkr_trade_observation([missing]),
                instrument_versions_by_conid={265598: "AAPL:v1"},
                fee_currency_by_execution_id={"exec-side-1": "USD"},
            )

        for invalid_side in ("UNKNOWN", "BUY", "SELL", "Buy", "b", "s"):
            with self.subTest(side=invalid_side):
                with self.assertRaisesRegex(
                    IbkrWebAdapterError,
                    "provider-evidenced",
                ):
                    parse_web_api_trades(
                        ibkr_trade_observation(
                            [dict(base, side=invalid_side)]
                        ),
                        instrument_versions_by_conid={265598: "AAPL:v1"},
                        fee_currency_by_execution_id={"exec-side-1": "USD"},
                    )

        fills = parse_web_api_trades(
            ibkr_trade_observation([dict(base, side="B")]),
            instrument_versions_by_conid={265598: "AAPL:v1"},
            fee_currency_by_execution_id={"exec-side-1": "USD"},
        )
        self.assertEqual(fills[0].side, "BUY")

    def test_reconciliation_fill_rejects_noncanonical_environment(self):
        execution = IbkrExecutionEvidence.create(
            execution_id="0001.998.01",
            permanent_order_id=778898,
            account_id="U1234567",
            quantity="1",
            price="100",
        )
        with self.assertRaisesRegex(ValueError, "environment"):
            execution_to_reconciliation_fill(
                execution,
                environment="UNKNOWN_ENV",
                client_order_id="at-ibkr-env",
                expected_account_id="U1234567",
                instrument="AAPL-CONID-265598:v1",
                fee_amount="0",
                fee_currency="USD",
                trade_time="2026-09-24T20:00:01Z",
                evidence_refs=EXECUTION_EVIDENCE_REFS,
            )

    def test_execution_cannot_cross_account_boundary_during_reconciliation(self):
        execution = IbkrExecutionEvidence.create(
            execution_id="0001.999.01",
            permanent_order_id=778899,
            account_id="U1234567",
            quantity="1",
            price="100",
        )
        with self.assertRaisesRegex(IbkrWebAdapterError, "account"):
            execution_to_reconciliation_fill(
                execution,
                client_order_id="at-ibkr-account",
                expected_account_id="OTHER",
                instrument="AAPL-CONID-265598:v1",
                fee_amount="0",
                fee_currency="USD",
                trade_time="2026-09-24T20:00:01Z",
                evidence_refs=EXECUTION_EVIDENCE_REFS,
            )

    def test_reconciliation_execution_requires_exact_evidence_type(self):
        class ExecutableExecution(IbkrExecutionEvidence):
            pass

        with self.assertRaisesRegex(TypeError, "exact IbkrExecutionEvidence"):
            execution_to_reconciliation_fill(
                object.__new__(ExecutableExecution),
                environment="PAPER",
                client_order_id="at-forged-execution",
                expected_account_id="U1234567",
                instrument="AAPL-CONID-265598:v1",
                fee_amount="0",
                fee_currency="USD",
                trade_time="2026-09-24T20:00:01Z",
                evidence_refs=EXECUTION_EVIDENCE_REFS,
            )

    def test_regulated_instrument_requires_manual_indicator_evidence(self):
        with self.assertRaisesRegex(IbkrWebAdapterError, "manual_indicator is required"):
            IbkrWebOrderIntent.create(
                instrument_version="AAPL-CONID-265598:v1",
                account_id="U1234567",
                contract=IbkrContractIdentity(conid=265598),
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity="1",
                regulatory_manual_indicator_required=True,
            )

    def test_automated_manual_indicator_false_is_preserved_when_required(self):
        intent = IbkrWebOrderIntent.create(
            instrument_version="AAPL-CONID-265598:v1",
            account_id="U1234567",
            contract=IbkrContractIdentity(conid=265598),
            side="BUY",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
            regulatory_manual_indicator_required=True,
            manual_indicator=False,
            ext_operator="autotrade",
        )
        prepared = prepare_normalized_order(
            intent,
            client_order_id="at-regulated-1",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        self.assertIs(prepared.fields["manualIndicator"], False)
        self.assertEqual(prepared.fields["extOperator"], "autotrade")



    def test_normalized_order_cannot_self_assert_provider_serialization_qualification(self):
        prepared = prepare_normalized_order(
            IbkrWebOrderIntent.create(
                instrument_version="AAPL-CONID-265598:v1",
                account_id="U1234567",
                contract=IbkrContractIdentity(conid=265598),
                side="BUY",
                order_type="MARKET",
                time_in_force="DAY",
                quantity="1",
            ),
            client_order_id="at-normalized-authority",
            capability=capability(),
            session=ready_session(),
            at=NOW,
            maximum_session_age_seconds=30,
        )
        with self.assertRaisesRegex(
            IbkrWebAdapterError, "cannot self-assert provider serialization qualification"
        ):
            IbkrNormalizedOrder(
                endpoint=prepared.endpoint,
                fields=dict(prepared.fields),
                exact_quantity_text=prepared.exact_quantity_text,
                exact_limit_price_text=prepared.exact_limit_price_text,
                exact_stop_price_text=prepared.exact_stop_price_text,
                capability_snapshot_id=prepared.capability_snapshot_id,
                documentation_refs=prepared.documentation_refs,
                provider_serialization_qualified=True,
            )

    def test_normalized_order_rejects_executable_mapping_before_callbacks(self):
        class ExecutableFields(dict):
            iter_called = False

            def __iter__(self):
                type(self).iter_called = True
                raise AssertionError("normalized field mapping callback executed")

            def keys(self):
                type(self).iter_called = True
                raise AssertionError("normalized field mapping callback executed")

            def __getitem__(self, key):
                type(self).iter_called = True
                raise AssertionError("normalized field mapping callback executed")

        fields = ExecutableFields(
            {
                "acctId": "U1234567",
                "orderType": "MKT",
                "side": "BUY",
                "tif": "DAY",
                "cOID": "at-hostile-fields",
                "conid": 265598,
            }
        )
        with self.assertRaisesRegex(TypeError, "fields must be an exact dict"):
            IbkrNormalizedOrder(
                endpoint="/iserver/account/U1234567/orders",
                fields=fields,
                exact_quantity_text="1",
                exact_limit_price_text=None,
                exact_stop_price_text=None,
                capability_snapshot_id="capability-1",
                documentation_refs=tuple(IBKR_WEB_DOCS.values()),
            )
        self.assertFalse(ExecutableFields.iter_called)

    def test_normalized_order_exact_numeric_evidence_requires_text(self):
        base = {
            "acctId": "U1234567",
            "orderType": "MKT",
            "side": "BUY",
            "tif": "DAY",
            "cOID": "at-exact-text",
            "conid": 265598,
        }
        with self.assertRaisesRegex(TypeError, "exact_quantity_text must be exact text"):
            IbkrNormalizedOrder(
                endpoint="/iserver/account/U1234567/orders",
                fields=base,
                exact_quantity_text=1,
                exact_limit_price_text=None,
                exact_stop_price_text=None,
                capability_snapshot_id="capability-1",
                documentation_refs=tuple(IBKR_WEB_DOCS.values()),
            )

    def test_normalized_order_rejects_shape_that_implies_unqualified_serialization(self):
        base = {
            "acctId": "U1234567",
            "orderType": "MKT",
            "side": "BUY",
            "tif": "DAY",
            "cOID": "at-unqualified-numeric",
            "conid": 265598,
        }
        with self.assertRaisesRegex(
            IbkrWebAdapterError, "supported IBKR shape"
        ):
            IbkrNormalizedOrder(
                endpoint="/iserver/account/U1234567/orders",
                fields={**base, "quantity": "1"},
                exact_quantity_text="1",
                exact_limit_price_text=None,
                exact_stop_price_text=None,
                capability_snapshot_id="capability-1",
                documentation_refs=tuple(IBKR_WEB_DOCS.values()),
            )

        with self.assertRaisesRegex(
            IbkrWebAdapterError, "price evidence does not match orderType"
        ):
            IbkrNormalizedOrder(
                endpoint="/iserver/account/U1234567/orders",
                fields=base,
                exact_quantity_text="1",
                exact_limit_price_text="100",
                exact_stop_price_text=None,
                capability_snapshot_id="capability-1",
                documentation_refs=tuple(IBKR_WEB_DOCS.values()),
            )

    def test_execution_evidence_direct_constructor_enforces_canonical_invariants(self):
        _HostileText.strip_called = False
        with self.assertRaisesRegex(IbkrWebAdapterError, "required|exact"):
            IbkrExecutionEvidence(
                execution_id=_HostileText("exec-direct"),
                permanent_order_id="778899",
                account_id="U1234567",
                quantity=Decimal("1"),
                price=Decimal("100"),
            )
        self.assertFalse(_HostileText.strip_called)

        with self.assertRaisesRegex(
            IbkrWebAdapterError, "canonical non-negative integer text"
        ):
            IbkrExecutionEvidence(
                execution_id="exec-direct",
                permanent_order_id="0778899",
                account_id="U1234567",
                quantity=Decimal("1"),
                price=Decimal("100"),
            )

        with self.assertRaisesRegex(
            IbkrWebAdapterError, "bounded exact decimal input"
        ):
            IbkrExecutionEvidence(
                execution_id="exec-direct",
                permanent_order_id="778899",
                account_id="U1234567",
                quantity=_HostileDecimal("1"),
                price=Decimal("100"),
            )

        direct = IbkrExecutionEvidence(
            execution_id="exec-direct",
            permanent_order_id="778899",
            account_id="U1234567",
            quantity=Decimal("1"),
            price=Decimal("100"),
        )
        created = IbkrExecutionEvidence.create(
            execution_id="exec-direct",
            permanent_order_id=778899,
            account_id="U1234567",
            quantity="1",
            price="100",
        )
        self.assertEqual(direct, created)

    def test_reconciliation_revalidates_mutated_execution_snapshot(self):
        execution = IbkrExecutionEvidence.create(
            execution_id="exec-mutated",
            permanent_order_id=778899,
            account_id="U1234567",
            quantity="1",
            price="100",
        )
        object.__setattr__(execution, "quantity", _HostileDecimal("1"))
        with self.assertRaisesRegex(
            IbkrWebAdapterError, "bounded exact decimal input"
        ):
            execution_to_reconciliation_fill(
                execution,
                client_order_id="at-exec-mutated",
                expected_account_id="U1234567",
                instrument="AAPL-CONID-265598:v1",
                fee_amount="0",
                fee_currency="USD",
                trade_time="2026-09-24T20:00:01Z",
                evidence_refs=EXECUTION_EVIDENCE_REFS,
            )

        execution = IbkrExecutionEvidence.create(
            execution_id="exec-mutated-id",
            permanent_order_id=778899,
            account_id="U1234567",
            quantity="1",
            price="100",
        )
        object.__setattr__(execution, "permanent_order_id", "0778899")
        with self.assertRaisesRegex(
            IbkrWebAdapterError, "canonical non-negative integer text"
        ):
            execution_to_reconciliation_fill(
                execution,
                client_order_id="at-exec-mutated-id",
                expected_account_id="U1234567",
                instrument="AAPL-CONID-265598:v1",
                fee_amount="0",
                fee_currency="USD",
                trade_time="2026-09-24T20:00:01Z",
                evidence_refs=EXECUTION_EVIDENCE_REFS,
            )

        _HostileText.strip_called = False
        execution = IbkrExecutionEvidence.create(
            execution_id="exec-mutated-account",
            permanent_order_id=778899,
            account_id="U1234567",
            quantity="1",
            price="100",
        )
        object.__setattr__(execution, "account_id", _HostileText("U1234567"))
        with self.assertRaisesRegex(IbkrWebAdapterError, "required|exact"):
            execution_to_reconciliation_fill(
                execution,
                client_order_id="at-exec-mutated-account",
                expected_account_id="U1234567",
                instrument="AAPL-CONID-265598:v1",
                fee_amount="0",
                fee_currency="USD",
                trade_time="2026-09-24T20:00:01Z",
                evidence_refs=EXECUTION_EVIDENCE_REFS,
            )
        self.assertFalse(_HostileText.strip_called)


if __name__ == "__main__":
    unittest.main()
