from collections.abc import Mapping
import copy
from dataclasses import replace
import unittest

from mvp.autotrade_mvp.financial_request_binding import FinancialRequestBindingMaterial
from mvp.autotrade_mvp.financial_send_authority import (
    FinancialSendAuthority,
    FinancialSendAuthorityError,
    FinancialSendAuthorityIssuer,
    FinanciallyBoundBybitOrderSender,
    require_exact_bybit_financial_request,
)
from mvp.autotrade_mvp.persistence import payload_digest


RS = "risk-snapshot:sha256:" + "1" * 64
RD = "risk:sha256:" + "2" * 64
AC = "provider-account-cut:sha256:" + "3" * 64
PS = "provider-financial-scope:sha256:" + "4" * 64
Q = "provider-qualification:sha256:" + "5" * 64
D6 = "sha256:" + "6" * 64
D7 = "sha256:" + "7" * 64
D8 = "sha256:" + "8" * 64


def exact_request():
    body = {
        "category": "linear",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderType": "Limit",
        "qty": "2",
        "timeInForce": "GTC",
        "orderLinkId": "client-order-1",
        "price": "30000",
        "reduceOnly": False,
        "positionIdx": 0,
    }
    body_sha = payload_digest(body)
    request = {
        "endpoint": "/v5/order/create",
        "body": body,
        "account_id": "account-1",
        "environment": "PAPER",
        "provider_environment": "TESTNET",
        "capability_snapshot_id": "capability-1",
        "entity_id": "entity-1",
        "capability_snapshot_ids": ["capability-1"],
        "instrument_versions": ["7"],
        "body_sha256": body_sha,
    }
    return request, body_sha


def exact_scope():
    return {
        "provider_id": "BYBIT",
        "account_id": "account-1",
        "environment": "PAPER",
        "provider_environment": "TESTNET",
        "capability_snapshot_id": "capability-1",
    }


def binding():
    request, body_sha = exact_request()
    scope = exact_scope()
    return FinancialRequestBindingMaterial(
        risk_snapshot_id=RS,
        risk_decision_id=RD,
        admitted_journal_sequence_cut=41,
        account_cut_id=AC,
        account_cut_digest=D6,
        account_head_journal_sequence=37,
        reservation_id="reservation-1",
        reservation_scope_digest=D7,
        reservation_version=3,
        reservation_state_digest=D8,
        provider_scope_digest=PS,
        provider_id="BYBIT",
        account_id="account-1",
        runtime_environment="PAPER",
        provider_environment="TESTNET",
        entity_policy_id="linear-order-v1",
        instrument_id="00000000-0000-0000-0000-000000000101",
        instrument_version=7,
        quantity_unit="CONTRACT",
        equivalent_exposure_digest=D6,
        capability_snapshot_id="capability-1",
        qualification_identity_digest=Q,
        client_order_id="client-order-1",
        side="BUY",
        quantity="2",
        price="30000",
        price_semantics_digest=D7,
        order_type="LIMIT",
        time_in_force="GTC",
        reduce_only=False,
        trigger_protection_digest=D8,
        endpoint="/v5/order/create",
        query_sha256=payload_digest({}),
        body_sha256=body_sha,
        request_sha256=payload_digest(request),
        submission_scope_digest=payload_digest(scope),
    )


class _RetargetingMapping(Mapping):
    """Expose one mapping on first materialization and another on any later one."""

    def __init__(self, first, later):
        self._first = dict(first)
        self._later = dict(later)
        self._active = self._first
        self.materializations = 0

    def __iter__(self):
        self.materializations += 1
        self._active = self._first if self.materializations == 1 else self._later
        return iter(self._active)

    def __len__(self):
        return len(self._active)

    def __getitem__(self, key):
        return self._active[key]


class _AuthorityStub:
    def __init__(self, material):
        self.binding = material
        self.intent_id = "intent-1"
        self.intent_hash = "intent-hash-1"


class _IssuerStub:
    def __init__(self, runtime):
        self.runtime = runtime

    def _dispatch_guard_for(self, _authority):
        return lambda _intent_hash, _now: (True, "allowed")


class _SenderStub:
    def __init__(self):
        self.request = None
        self.submission_scope = None

    def dispatch(self, **kwargs):
        self.request = dict(kwargs["request"])
        self.submission_scope = dict(kwargs["submission_scope"])
        return "sent"


def _bound_sender_harness(*, executable_stub: bool = False):
    from mvp.autotrade_mvp import financial_send_authority as module

    runtime = object()
    lower = _SenderStub()
    issuer = _IssuerStub(runtime)
    bound = object.__new__(FinanciallyBoundBybitOrderSender)
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__sender",
        lower,
    )
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__issuer",
        issuer,
    )
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__runtime",
        runtime,
    )
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__provider_environment",
        "TESTNET",
    )
    issuer_guard = (
        _IssuerStub._dispatch_guard_for
        if executable_stub
        else FinancialSendAuthorityIssuer._dispatch_guard_for
    )
    pins = {
        "__issuer_guard_function": issuer_guard,
        "__issuer_guard_code": issuer_guard.__code__,
        "__request_guard_function": module.require_exact_bybit_financial_request,
        "__request_guard_code": module.require_exact_bybit_financial_request.__code__,
        "__snapshot_function": module._detached_mapping_snapshot,
        "__snapshot_code": module._detached_mapping_snapshot.__code__,
        "__mapping_digest_function": module._mapping_digest,
        "__mapping_digest_code": module._mapping_digest.__code__,
        "__exact_text_function": module._exact_text,
        "__exact_text_code": module._exact_text.__code__,
    }
    for suffix, value in pins.items():
        object.__setattr__(
            bound,
            f"_FinanciallyBoundBybitOrderSender{suffix}",
            value,
        )
    return bound, lower


def _exercise_stub_bound_sender(bound, **kwargs):
    original = FinanciallyBoundBybitOrderSender._require_composition_current
    FinanciallyBoundBybitOrderSender._require_composition_current = lambda _self: None
    try:
        return bound.dispatch(**kwargs)
    finally:
        FinanciallyBoundBybitOrderSender._require_composition_current = original


class ExactBybitFinancialRequestTests(unittest.TestCase):
    def test_exact_prepared_projection_and_scope_are_admitted(self):
        request, _ = exact_request()
        require_exact_bybit_financial_request(
            binding(),
            request,
            exact_scope(),
            provider_environment="TESTNET",
        )

    def test_request_retarget_fails_even_when_body_shape_stays_valid(self):
        request, _ = exact_request()
        retargeted = dict(request)
        retargeted["account_id"] = "account-2"
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "request digest differs",
        ):
            require_exact_bybit_financial_request(
                binding(),
                retargeted,
                exact_scope(),
                provider_environment="TESTNET",
            )

    def test_economics_retarget_fails_against_binding_even_with_rebound_request_digest(self):
        request, _ = exact_request()
        changed_body = dict(request["body"])
        changed_body["qty"] = "3"
        changed_request = dict(request)
        changed_request["body"] = changed_body
        changed_request["body_sha256"] = payload_digest(changed_body)
        rebound = replace(
            binding(),
            request_sha256=payload_digest(changed_request),
            body_sha256=payload_digest(changed_body),
        )
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "quantity differs",
        ):
            require_exact_bybit_financial_request(
                rebound,
                changed_request,
                exact_scope(),
                provider_environment="TESTNET",
            )

    def test_submission_scope_retarget_fails_closed(self):
        request, _ = exact_request()
        retargeted_scope = dict(exact_scope())
        retargeted_scope["provider_environment"] = "DEMO"
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "submission scope differs",
        ):
            require_exact_bybit_financial_request(
                binding(),
                request,
                retargeted_scope,
                provider_environment="TESTNET",
            )

    def test_provider_environment_retarget_fails_before_dispatch(self):
        request, _ = exact_request()
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "provider environment differs",
        ):
            require_exact_bybit_financial_request(
                binding(),
                request,
                exact_scope(),
                provider_environment="DEMO",
            )

    def test_bound_sender_detaches_stateful_request_before_financial_check(self):
        admitted, _ = exact_request()
        retargeted = dict(admitted)
        retargeted["account_id"] = "account-2"
        stateful_request = _RetargetingMapping(admitted, retargeted)
        bound, lower = _bound_sender_harness(executable_stub=True)

        result = _exercise_stub_bound_sender(
            bound,
            authority=_AuthorityStub(binding()),
            attempt_id="attempt-1",
            intent_id="intent-1",
            intent_hash="intent-hash-1",
            request=stateful_request,
            now="2026-10-04T04:30:00Z",
            submission_scope=exact_scope(),
        )

        self.assertEqual(result, "sent")
        self.assertEqual(stateful_request.materializations, 1)
        self.assertEqual(lower.request["account_id"], "account-1")

    def test_bound_sender_detaches_stateful_submission_scope_before_dispatch(self):
        admitted_scope = exact_scope()
        retargeted_scope = dict(admitted_scope)
        retargeted_scope["provider_environment"] = "DEMO"
        stateful_scope = _RetargetingMapping(admitted_scope, retargeted_scope)
        bound, lower = _bound_sender_harness(executable_stub=True)

        result = _exercise_stub_bound_sender(
            bound,
            authority=_AuthorityStub(binding()),
            attempt_id="attempt-2",
            intent_id="intent-1",
            intent_hash="intent-hash-1",
            request=exact_request()[0],
            now="2026-10-04T04:30:00Z",
            submission_scope=stateful_scope,
        )

        self.assertEqual(result, "sent")
        self.assertEqual(stateful_scope.materializations, 1)
        self.assertEqual(
            lower.submission_scope["provider_environment"],
            "TESTNET",
        )

    def test_direct_capability_construction_is_rejected(self):
        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "must be minted",
        ):
            FinancialSendAuthority(
                issuer_identity=object(),
                binding=binding(),
                admission_id="admission-1",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                action="TRADE",
            )

    def test_importable_capability_factory_token_is_not_a_minting_boundary(self):
        from mvp.autotrade_mvp import financial_send_authority as module

        with self.assertRaisesRegex(
            FinancialSendAuthorityError,
            "must be minted",
        ):
            FinancialSendAuthority(
                issuer_identity=object(),
                binding=binding(),
                admission_id="admission-1",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                action="TRADE",
                _factory_token=module._CAPABILITY_FACTORY_TOKEN,
            )

    def test_minted_shape_is_immutable_and_noncopyable(self):
        authority = object.__new__(FinancialSendAuthority)
        object.__setattr__(
            authority,
            "_FinancialSendAuthority__issuer_identity",
            object(),
        )
        object.__setattr__(authority, "_FinancialSendAuthority__binding", binding())
        object.__setattr__(
            authority,
            "_FinancialSendAuthority__admission_id",
            "admission-1",
        )
        object.__setattr__(
            authority,
            "_FinancialSendAuthority__intent_id",
            "intent-1",
        )
        object.__setattr__(
            authority,
            "_FinancialSendAuthority__intent_hash",
            "intent-hash-1",
        )
        object.__setattr__(authority, "_FinancialSendAuthority__action", "TRADE")

        with self.assertRaisesRegex(FinancialSendAuthorityError, "immutable"):
            authority._FinancialSendAuthority__intent_hash = "retargeted"
        with self.assertRaisesRegex(FinancialSendAuthorityError, "cannot be copied"):
            copy.copy(authority)

    def test_bound_sender_rejects_post_composition_retarget(self):
        bound, _lower = _bound_sender_harness()
        with self.assertRaisesRegex(FinancialSendAuthorityError, "immutable"):
            bound._FinanciallyBoundBybitOrderSender__provider_environment = "DEMO"

    def test_bound_sender_rejects_request_guard_rebinding_before_lower_dispatch(self):
        from mvp.autotrade_mvp import financial_send_authority as module

        bound, lower = _bound_sender_harness()
        original = module.require_exact_bybit_financial_request
        module.require_exact_bybit_financial_request = lambda *_args, **_kwargs: None
        try:
            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "request authority changed",
            ):
                bound.dispatch(
                    authority=_AuthorityStub(binding()),
                    attempt_id="attempt-rebound",
                    intent_id="intent-1",
                    intent_hash="intent-hash-1",
                    request=exact_request()[0],
                    now="2026-10-04T04:30:00Z",
                    submission_scope=exact_scope(),
                )
            self.assertIsNone(lower.request)
        finally:
            module.require_exact_bybit_financial_request = original


if __name__ == "__main__":
    unittest.main()
