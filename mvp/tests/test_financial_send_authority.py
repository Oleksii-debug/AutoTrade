from collections.abc import Mapping
from dataclasses import replace
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.financial_request_binding import FinancialRequestBindingMaterial
from mvp.autotrade_mvp.financial_send_authority import (
    FinancialSendAuthority,
    FinancialSendAuthorityError,
    FinancialSendAuthorityIssuer,
    FinanciallyBoundBybitOrderSender,
    require_exact_bybit_financial_request,
)
from mvp.autotrade_mvp.persistence import payload_digest
from mvp.autotrade_mvp.production_bybit import ProductionBybitOrderSender


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
    request, _body_sha = exact_request()
    return {
        "provider_id": "BYBIT",
        "account_id": "account-1",
        "environment": "PAPER",
        "provider_environment": "TESTNET",
        "capability_snapshot_id": "capability-1",
        "endpoint": "/v5/order/create",
        "prepared_request_sha256": payload_digest(request),
        "capability_snapshot_ids": ["capability-1"],
        "instrument_versions": ["7"],
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


class _HostileMapping(Mapping):
    callbacks = 0

    def __init__(self, payload):
        self._payload = dict(payload)

    @classmethod
    def reset(cls):
        cls.callbacks = 0

    def __iter__(self):
        type(self).callbacks += 1
        raise AssertionError("hostile mapping iteration executed")

    def __len__(self):
        type(self).callbacks += 1
        raise AssertionError("hostile mapping length executed")

    def __getitem__(self, _key):
        type(self).callbacks += 1
        raise AssertionError("hostile mapping lookup executed")


class _HostileDict(dict):
    callbacks = 0

    @classmethod
    def reset(cls):
        cls.callbacks = 0

    @classmethod
    def _boom(cls):
        cls.callbacks += 1
        raise AssertionError("hostile dict callback executed")

    def __iter__(self):
        type(self)._boom()

    def __getitem__(self, _key):
        type(self)._boom()

    def get(self, _key, _default=None):
        type(self)._boom()

    def items(self):
        type(self)._boom()


class _HostileText(str):
    callbacks = 0

    @classmethod
    def reset(cls):
        cls.callbacks = 0

    def __str__(self):
        type(self).callbacks += 1
        raise AssertionError("hostile text callback executed")

    def __eq__(self, _other):
        type(self).callbacks += 1
        raise AssertionError("hostile text callback executed")

    def __hash__(self):
        type(self).callbacks += 1
        raise AssertionError("hostile text callback executed")


class _HostileDescriptor:
    callbacks = 0

    @classmethod
    def reset(cls):
        cls.callbacks = 0

    def __get__(self, _instance, _owner):
        type(self).callbacks += 1
        raise AssertionError("hostile capability descriptor executed")


class _AuthorityStub:
    def __init__(self, material):
        self.binding = material
        self.intent_id = "intent-1"
        self.intent_hash = "intent-hash-1"


class _OpaqueAuthority:
    @property
    def binding(self):
        raise AssertionError("bound sender dynamically read authority.binding")

    @property
    def intent_id(self):
        raise AssertionError("bound sender dynamically read authority.intent_id")

    @property
    def intent_hash(self):
        raise AssertionError("bound sender dynamically read authority.intent_hash")


class _IssuerStub:
    def __init__(self, runtime, *, before_guard=None):
        self.runtime = runtime
        self.before_guard = before_guard

    def _dispatch_material_for(self, _authority):
        if self.before_guard is not None:
            self.before_guard()
        return (
            lambda _intent_hash, _now: (True, "allowed"),
            binding(),
            "intent-1",
            "intent-hash-1",
        )


class _SenderStub:
    def __init__(self):
        self.request = None
        self.submission_scope = None

    def dispatch(self, **kwargs):
        self.request = dict(kwargs["request"])
        self.submission_scope = dict(kwargs["submission_scope"])
        return "sent"


def _bound_sender_harness(*, before_guard=None):
    runtime = object()
    lower = _SenderStub()
    bound = object.__new__(FinanciallyBoundBybitOrderSender)
    stub_dispatch_function = _SenderStub.dispatch
    stub_dispatch = lower.dispatch
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__sender",
        lower,
    )
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__sender_dispatch",
        stub_dispatch,
    )
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__sender_dispatch_function",
        stub_dispatch_function,
    )
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__sender_dispatch_code",
        stub_dispatch_function.__code__,
    )
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__issuer",
        _IssuerStub(runtime, before_guard=before_guard),
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
    return bound, lower


def _executable_authority_shell():
    sender = object()
    function = ProductionBybitOrderSender.dispatch
    bound_method = function.__get__(sender, ProductionBybitOrderSender)
    bound = object.__new__(FinanciallyBoundBybitOrderSender)
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__sender",
        sender,
    )
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__sender_dispatch",
        bound_method,
    )
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__sender_dispatch_function",
        function,
    )
    object.__setattr__(
        bound,
        "_FinanciallyBoundBybitOrderSender__sender_dispatch_code",
        function.__code__,
    )
    return bound


def _capability_executable_authority_shell():
    issuer = object.__new__(FinancialSendAuthorityIssuer)
    issuer_function = FinancialSendAuthority.__dict__["_require_issuer"]
    property_authorities = []
    for name in ("binding", "admission_id", "intent_id", "intent_hash", "action"):
        descriptor = FinancialSendAuthority.__dict__[name]
        getter = descriptor.fget
        property_authorities.append((name, descriptor, getter, getter.__code__))
    object.__setattr__(
        issuer,
        "_FinancialSendAuthorityIssuer__capability_issuer_function",
        issuer_function,
    )
    object.__setattr__(
        issuer,
        "_FinancialSendAuthorityIssuer__capability_issuer_code",
        issuer_function.__code__,
    )
    object.__setattr__(
        issuer,
        "_FinancialSendAuthorityIssuer__capability_property_authorities",
        tuple(property_authorities),
    )
    return issuer


def _forged_sender_dispatch(*_args, **_kwargs):
    raise AssertionError("forged sender dispatch executed")


def _forged_capability_issuer(_self, _issuer_identity):
    raise AssertionError("forged capability issuer executable ran")


def _forged_capability_getter(_self):
    raise AssertionError("forged capability getter executable ran")


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

    def test_mapping_request_is_rejected_without_callbacks(self):
        _HostileMapping.reset()
        hostile = _HostileMapping(exact_request()[0])
        with self.assertRaisesRegex(TypeError, "request must be an exact dict"):
            require_exact_bybit_financial_request(
                binding(),
                hostile,
                exact_scope(),
                provider_environment="TESTNET",
            )
        self.assertEqual(_HostileMapping.callbacks, 0)

    def test_request_dict_subclass_is_rejected_without_callbacks(self):
        _HostileDict.reset()
        hostile = _HostileDict(exact_request()[0])
        with self.assertRaisesRegex(TypeError, "request must be an exact dict"):
            require_exact_bybit_financial_request(
                binding(),
                hostile,
                exact_scope(),
                provider_environment="TESTNET",
            )
        self.assertEqual(_HostileDict.callbacks, 0)

    def test_nested_body_dict_subclass_is_rejected_without_callbacks(self):
        request, _ = exact_request()
        _HostileDict.reset()
        request["body"] = _HostileDict(request["body"])
        with self.assertRaisesRegex(
            TypeError,
            "request.body must contain exact JSON-domain values",
        ):
            require_exact_bybit_financial_request(
                binding(),
                request,
                exact_scope(),
                provider_environment="TESTNET",
            )
        self.assertEqual(_HostileDict.callbacks, 0)

    def test_submission_scope_dict_subclass_is_rejected_without_callbacks(self):
        request, _ = exact_request()
        _HostileDict.reset()
        hostile_scope = _HostileDict(exact_scope())
        with self.assertRaisesRegex(
            TypeError,
            "submission_scope must be an exact dict",
        ):
            require_exact_bybit_financial_request(
                binding(),
                request,
                hostile_scope,
                provider_environment="TESTNET",
            )
        self.assertEqual(_HostileDict.callbacks, 0)

    def test_nested_text_subclass_is_rejected_without_callbacks(self):
        request, _ = exact_request()
        _HostileText.reset()
        request["body"]["symbol"] = _HostileText("BTCUSDT")
        with self.assertRaisesRegex(
            TypeError,
            "request.body.symbol must contain exact JSON-domain values",
        ):
            require_exact_bybit_financial_request(
                binding(),
                request,
                exact_scope(),
                provider_environment="TESTNET",
            )
        self.assertEqual(_HostileText.callbacks, 0)

    def test_bound_sender_detaches_exact_request_before_authority_callback(self):
        request, _ = exact_request()

        def retarget_original():
            request["account_id"] = "account-2"

        bound, lower = _bound_sender_harness(before_guard=retarget_original)
        with patch.object(ProductionBybitOrderSender, "dispatch", _SenderStub.dispatch):
            result = bound.dispatch(
                authority=_AuthorityStub(binding()),
                attempt_id="attempt-1",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                request=request,
                now="2026-10-04T04:30:00Z",
                submission_scope=exact_scope(),
            )

        self.assertEqual(result, "sent")
        self.assertEqual(request["account_id"], "account-2")
        self.assertEqual(lower.request["account_id"], "account-1")

    def test_bound_sender_detaches_exact_scope_before_authority_callback(self):
        scope = exact_scope()

        def retarget_original():
            scope["provider_environment"] = "DEMO"

        bound, lower = _bound_sender_harness(before_guard=retarget_original)
        with patch.object(ProductionBybitOrderSender, "dispatch", _SenderStub.dispatch):
            result = bound.dispatch(
                authority=_AuthorityStub(binding()),
                attempt_id="attempt-2",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                request=exact_request()[0],
                now="2026-10-04T04:30:00Z",
                submission_scope=scope,
            )

        self.assertEqual(result, "sent")
        self.assertEqual(scope["provider_environment"], "DEMO")
        self.assertEqual(lower.submission_scope["provider_environment"], "TESTNET")

    def test_bound_sender_never_dereferences_caller_capability(self):
        bound, lower = _bound_sender_harness()
        with patch.object(ProductionBybitOrderSender, "dispatch", _SenderStub.dispatch):
            result = bound.dispatch(
                authority=_OpaqueAuthority(),
                attempt_id="attempt-3",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                request=exact_request()[0],
                now="2026-10-04T04:30:00Z",
                submission_scope=exact_scope(),
            )
        self.assertEqual(result, "sent")
        self.assertEqual(lower.request["account_id"], "account-1")

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

    def test_importable_factory_token_cannot_inject_authority_callback(self):
        from mvp.autotrade_mvp import financial_send_authority as module

        with self.assertRaises(TypeError):
            FinancialSendAuthority(
                issuer_identity=object(),
                binding=binding(),
                admission_id="admission-1",
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                action="TRADE",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                _factory_token=module._CAPABILITY_FACTORY_TOKEN,
            )


class CapabilityExecutableAuthorityTests(unittest.TestCase):
    def test_issuer_method_rebinding_fails_before_forged_executable(self):
        issuer = _capability_executable_authority_shell()
        original = FinancialSendAuthority.__dict__["_require_issuer"]
        try:
            setattr(
                FinancialSendAuthority,
                "_require_issuer",
                _forged_capability_issuer,
            )
            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "issuer executable authority changed",
            ):
                issuer._require_capability_executable_authority()
        finally:
            setattr(FinancialSendAuthority, "_require_issuer", original)

    def test_property_descriptor_rebinding_does_not_execute_descriptor(self):
        issuer = _capability_executable_authority_shell()
        original = FinancialSendAuthority.__dict__["binding"]
        hostile = _HostileDescriptor()
        _HostileDescriptor.reset()
        try:
            setattr(FinancialSendAuthority, "binding", hostile)
            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "binding property authority changed",
            ):
                issuer._require_capability_executable_authority()
        finally:
            setattr(FinancialSendAuthority, "binding", original)
        self.assertEqual(_HostileDescriptor.callbacks, 0)

    def test_property_getter_same_function_code_mutation_fails_closed(self):
        issuer = _capability_executable_authority_shell()
        descriptor = FinancialSendAuthority.__dict__["intent_hash"]
        getter = descriptor.fget
        original_code = getter.__code__
        try:
            getter.__code__ = _forged_capability_getter.__code__
            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "intent_hash getter authority code changed",
            ):
                issuer._require_capability_executable_authority()
        finally:
            getter.__code__ = original_code


class BoundBybitExecutableAuthorityTests(unittest.TestCase):
    def test_sender_dispatch_rebinding_fails_before_forged_executable(self):
        bound = _executable_authority_shell()
        calls = []

        def forged(*_args, **_kwargs):
            calls.append("forged")
            raise AssertionError("forged sender dispatch executed")

        with patch.object(ProductionBybitOrderSender, "dispatch", forged):
            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "dispatch executable authority changed",
            ):
                bound._require_sender_dispatch_authority()
        self.assertEqual(calls, [])

    def test_sender_dispatch_same_function_code_mutation_fails_closed(self):
        bound = _executable_authority_shell()
        dispatch = ProductionBybitOrderSender.dispatch
        original_code = dispatch.__code__
        try:
            dispatch.__code__ = _forged_sender_dispatch.__code__
            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "dispatch executable authority code changed",
            ):
                bound._require_sender_dispatch_authority()
        finally:
            dispatch.__code__ = original_code


if __name__ == "__main__":
    unittest.main()
