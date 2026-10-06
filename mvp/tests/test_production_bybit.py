from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import production_host
from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.bybit_v5 import (
    guarded_order_projection,
    guarded_order_request_sha256,
    parse_submission_response,
    prepare_order_submission,
)
from mvp.autotrade_mvp.capabilities import CapabilityRegistry
from mvp.autotrade_mvp.dispatch import (
    load_submission_response_binding,
    stable_client_order_id,
)
from mvp.autotrade_mvp.durable_financial_bybit_sender import (
    DurableFinanciallyBoundBybitOrderSender,
)
from mvp.autotrade_mvp.durable_financial_request_binding import (
    DurableFinancialRequestBindingRegistry,
)
from mvp.autotrade_mvp.host_network import AuthenticatedHostApplication
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.financial_send_authority import (
    FinancialSendAuthorityError,
    FinanciallyBoundBybitOrderSender,
    build_financial_send_authority_issuer,
)
from mvp.autotrade_mvp.production_bybit import (
    ProductionBybitOrderSender,
    _build_production_bybit_order_sender,
    build_production_bybit_order_sender,
)
from mvp.autotrade_mvp.production_financial_host import compose_financial_authority
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.provider_core import observe_submission_json_response
from mvp.autotrade_mvp.provider_transport import ProviderTransportScopeError
from mvp.autotrade_mvp.recovery import HostState, RecoveryController
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault
from mvp.tests.test_security import DeterministicProtector
from mvp.tests.test_bybit_v5 import READ_AT, write_capability
from mvp.tests.test_reconciliation_journal import reconciliation


_NOW = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)


class _FenceStub:
    def __init__(self) -> None:
        self.released = False


class _RecordingWire:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return self.response


class ProductionBybitCurrentHostTests(unittest.TestCase):
    def _runtime(
        self,
        root: str,
        *,
        environment: str = "PAPER",
        host_id: str = "host-a",
        journal: JournalStore | None = None,
        security_boundary: SecurityBoundary | None = None,
    ):
        journal = journal or JournalStore(Path(root) / "financial-host.sqlite")
        config = ProductionHostConfig(
            journal_path=journal.path,
            account_id="account-1",
            environment=environment,
            host_id=host_id,
            bind_host="127.0.0.1",
            bind_port=18765,
            public_origin="http://127.0.0.1:18765",
        )
        boundary = security_boundary or object.__new__(SecurityBoundary)
        application = object.__new__(AuthenticatedHostApplication)
        application.security_boundary = boundary
        host = ProductionHostRuntime(
            config=config,
            journal=journal,
            application=application,
            server=object(),
            instance_fence=_FenceStub(),
            admission_gate=object(),
            issuance_token=production_host._RUNTIME_ISSUANCE_TOKEN,
        )
        return compose_financial_authority(host), host, boundary

    @staticmethod
    def _handle(
        *,
        environment: str = "PAPER",
        account_id: str = "account-1",
        provider_environment: str = "TESTNET",
    ) -> PersistentCredentialHandle:
        return PersistentCredentialHandle(
            handle_id="cred-bybit-trade",
            account_id=account_id,
            provider="BYBIT",
            environment=environment,
            provider_environment=provider_environment,
            purpose="TRADE",
            generation=1,
        )

    def _sender(
        self,
        runtime,
        *,
        provider_environment: str = "TESTNET",
        credential_handle: PersistentCredentialHandle | None = None,
        capability_registry: CapabilityRegistry | None = None,
        capability_snapshot_id: str = "capability-1",
        wire_client=None,
        session_token: str = "session-1",
        quota_gate=None,
        clock_utc=None,
    ) -> ProductionBybitOrderSender:
        return _build_production_bybit_order_sender(
            runtime,
            provider_environment=provider_environment,
            capability_snapshot_id=capability_snapshot_id,
            capability_registry=capability_registry or CapabilityRegistry(),
            credential_handle=credential_handle or self._handle(),
            session_token=session_token,
            clock_millis=lambda: 1_700_000_000_000,
            clock_utc=clock_utc or (lambda: _NOW),
            quota_gate=quota_gate,
            wire_client=wire_client,
        )

    def _mark_ready(
        self,
        runtime,
        *,
        provider_id: str = "BYBIT",
        reconciliation_id: str = "production-bybit-ready",
    ) -> None:
        recovery = runtime.recovery_controller
        owner = recovery.owner
        self.assertIsNotNone(owner)
        result = reconciliation(
            provider_id=provider_id,
            account_id=runtime.config.account_id,
            environment=runtime.config.environment,
        )
        record_reconciliation_checkpoint(
            runtime.journal,
            reconciliation_id=reconciliation_id,
            result=result,
            observed_at="2026-10-04T01:59:59Z",
            host_id=owner.owner_id,
            owner_epoch=str(owner.epoch),
        )
        recovery.record_reconciliation_checkpoint(
            reconciliation_id=reconciliation_id,
            provider_id=provider_id,
            account_id=runtime.config.account_id,
            environment=runtime.config.environment,
        )
        self.assertIs(recovery.state, HostState.READY)

    def test_raw_sender_direct_construction_is_not_a_product_surface(self) -> None:
        with self.assertRaisesRegex(
            PermissionError,
            "requires internal financial composition",
        ):
            ProductionBybitOrderSender(runtime=object(), transport=object())

    def test_public_builder_rejects_financial_issuer_without_selected_route(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _host, _boundary = self._runtime(root)
            service = AuthorityService(runtime.journal)
            issuer = build_financial_send_authority_issuer(service, runtime)
            with self.assertRaisesRegex(
                FinancialSendAuthorityError,
                "requires selected provider route authority",
            ):
                build_production_bybit_order_sender(
                    runtime,
                    financial_issuer=issuer,
                    financial_binding_registry=DurableFinancialRequestBindingRegistry(runtime.journal),
                    provider_environment="TESTNET",
                    capability_snapshot_id="capability-1",
                    capability_registry=CapabilityRegistry(),
                    credential_handle=self._handle(),
                    session_token="session-1",
                    clock_millis=lambda: 1_700_000_000_000,
                    clock_utc=lambda: _NOW,
                )

    def test_public_builder_rejects_noncanonical_financial_issuer_before_raw_build(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _host, _boundary = self._runtime(root)
            with self.assertRaisesRegex(
                TypeError,
                "financial_issuer must be exact",
            ):
                build_production_bybit_order_sender(
                    runtime,
                    financial_issuer=object(),
                    financial_binding_registry=DurableFinancialRequestBindingRegistry(runtime.journal),
                    provider_environment="TESTNET",
                    capability_snapshot_id="capability-1",
                    capability_registry=CapabilityRegistry(),
                    credential_handle=self._handle(),
                    session_token="session-1",
                    clock_millis=lambda: 1_700_000_000_000,
                    clock_utc=lambda: _NOW,
                )

    def test_product_modules_do_not_reach_raw_callback_sender_surface(self) -> None:
        product_root = Path(__file__).resolve().parents[1] / "autotrade_mvp"
        allowed = {"production_bybit.py", "financial_send_authority.py"}
        forbidden = {
            "ProductionBybitOrderSender",
            "_build_production_bybit_order_sender",
        }
        violations = []
        for source_path in sorted(product_root.glob("*.py")):
            if source_path.name in allowed:
                continue
            source = source_path.read_text(encoding="utf-8")
            for token in sorted(forbidden):
                if token in source:
                    violations.append(f"{source_path.name}:{token}")

        self.assertEqual(violations, [])
        public_arguments = build_production_bybit_order_sender.__code__.co_varnames[
            : (
                build_production_bybit_order_sender.__code__.co_argcount
                + build_production_bybit_order_sender.__code__.co_kwonlyargcount
            )
        ]
        self.assertIn("financial_issuer", public_arguments)
        self.assertIn("financial_binding_registry", public_arguments)
        self.assertNotIn("authority", public_arguments)
        self.assertNotIn("binding", public_arguments)
        self.assertNotIn("authority_check", public_arguments)

        bound_arguments = (
            DurableFinanciallyBoundBybitOrderSender.dispatch.__code__.co_varnames[
                : (
                    DurableFinanciallyBoundBybitOrderSender.dispatch.__code__.co_argcount
                    + DurableFinanciallyBoundBybitOrderSender.dispatch.__code__.co_kwonlyargcount
                )
            ]
        )
        self.assertIn("admission_id", bound_arguments)
        self.assertIn("action", bound_arguments)
        self.assertNotIn("authority", bound_arguments)
        self.assertNotIn("binding", bound_arguments)
        self.assertNotIn("authority_check", bound_arguments)
        self.assertNotIn("final_barrier_clock", bound_arguments)

    def test_builder_uses_exact_host_security_boundary_and_financial_scope(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _host, boundary = self._runtime(root)
            sender = self._sender(runtime)
            transport = sender._ProductionBybitOrderSender__transport
            resolver = transport.secret_resolver

            self.assertEqual(sender.provider_environment, "TESTNET")
            self.assertEqual(transport.account_id, "account-1")
            self.assertEqual(transport.policy.environment, "PAPER")
            self.assertEqual(transport.origin, runtime.config.public_origin)
            self.assertEqual(
                transport.execution_identity,
                runtime.financial_dispatcher.owner.owner_id,
            )
            self.assertIs(resolver.security_boundary, boundary)
            self.assertIs(
                runtime.application.security_boundary,
                resolver.security_boundary,
            )

    def test_product_environment_cannot_be_retargeted_by_provider_domain(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _host, _boundary = self._runtime(root)
            with self.assertRaisesRegex(
                PermissionError,
                "provider environment does not match production host environment",
            ):
                self._sender(
                    runtime,
                    provider_environment="MAINNET",
                    credential_handle=self._handle(
                        environment="LIVE",
                        provider_environment="MAINNET",
                    ),
                )

        with TemporaryDirectory() as root:
            runtime, _host, _boundary = self._runtime(root, environment="LIVE")
            with self.assertRaisesRegex(
                PermissionError,
                "provider environment does not match production host environment",
            ):
                self._sender(runtime, provider_environment="TESTNET")

    def test_provider_environment_must_be_exact_canonical_domain(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _host, _boundary = self._runtime(root)
            with self.assertRaisesRegex(
                ValueError,
                "MAINNET, TESTNET or DEMO",
            ):
                self._sender(runtime, provider_environment="testnet")

    def test_credential_handle_scope_is_enforced_by_existing_transport(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _host, _boundary = self._runtime(root)
            with self.assertRaisesRegex(
                ProviderTransportScopeError,
                "credential handle account mismatch",
            ):
                self._sender(
                    runtime,
                    credential_handle=self._handle(account_id="other-account"),
                )

    def test_resolver_rejects_caller_scope_substitution_before_secret_access(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _host, boundary = self._runtime(root)
            sender = self._sender(runtime)
            transport = sender._ProductionBybitOrderSender__transport
            resolver = transport.secret_resolver
            lease_calls = []

            @contextmanager
            def fake_lease(_self, token, **kwargs):
                lease_calls.append((token, kwargs))
                yield "secret"

            original = SecurityBoundary.lease_for_execution
            SecurityBoundary.lease_for_execution = fake_lease
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "production credential lease authority changed",
                ):
                    with resolver.lease_for_execution(
                        "session-1",
                        origin=runtime.config.public_origin,
                        handle=self._handle(),
                        execution_identity=runtime.financial_dispatcher.owner.owner_id,
                        account_id="other-account",
                        provider="BYBIT",
                        environment="PAPER",
                        purpose="TRADE",
                        provider_environment="TESTNET",
                    ):
                        self.fail("cross-account credential scope was accepted")
                self.assertEqual(lease_calls, [])
                self.assertIs(runtime.application.security_boundary, boundary)
            finally:
                SecurityBoundary.lease_for_execution = original

    def test_existing_durable_owner_requires_takeover_before_bybit_sender_exists(self) -> None:
        with TemporaryDirectory() as root:
            journal = JournalStore(Path(root) / "financial-host.sqlite")
            old = RecoveryController(
                owner_store=journal,
                owner_scope="PAPER:account-1",
            )
            old.start("host-old")
            runtime, _host, _boundary = self._runtime(
                root,
                host_id="host-new",
                journal=journal,
            )

            self.assertTrue(runtime.takeover_required)
            with self.assertRaisesRegex(
                PermissionError,
                "until explicit durable takeover completes",
            ):
                self._sender(runtime)

    def test_host_closing_blocks_before_secret_or_wire_side_effects(self) -> None:
        with TemporaryDirectory() as root:
            runtime, host, _boundary = self._runtime(root)
            wire = _RecordingWire(b'{"retCode":0,"retMsg":"OK","result":{}}')
            sender = self._sender(runtime, wire_client=wire)
            host._serve_state = "CLOSING"
            before = runtime.journal.load_events_by_aggregate_type("submission_attempt")

            with self.assertRaisesRegex(
                PermissionError,
                "closing or closed",
            ):
                sender.dispatch(
                    attempt_id="attempt-closing",
                    intent_id="intent-closing",
                    intent_hash="sha256:" + "1" * 64,
                    request={"symbol": "BTCUSDT"},
                    now="2026-10-04T02:00:00Z",
                    authority_check=lambda _intent_hash, _now: (True, "allowed"),
                )

            self.assertEqual(wire.requests, [])
            self.assertEqual(
                runtime.journal.load_events_by_aggregate_type("submission_attempt"),
                before,
            )

    def test_capability_expiry_during_quota_wait_is_zero_wire(self) -> None:
        with TemporaryDirectory() as root:
            runtime, _host, _boundary = self._runtime(root)
            capability = write_capability(
                family="LINEAR_DERIVATIVES",
                position_mode="HEDGE",
                account_id="account-1",
                environment="PAPER",
                instrument_version="BTCUSDT@1",
                permission_scope="BYBIT.LINEAR.ORDER.WRITE",
                additional_permission_scopes=("ORDER_WRITE",),
                provider_environment="TESTNET",
                expires_at=_NOW + timedelta(minutes=5),
            )
            registry = CapabilityRegistry()
            registry.add(capability)
            current = [_NOW]
            quota_calls = 0
            wire = _RecordingWire(
                b'{"retCode":0,"retMsg":"OK","result":{"orderId":"never"}}'
            )

            def quota_gate(*_args):
                nonlocal quota_calls
                quota_calls += 1
                current[0] = _NOW + timedelta(minutes=6)

            sender = self._sender(
                runtime,
                capability_registry=registry,
                capability_snapshot_id=capability.snapshot_id,
                wire_client=wire,
                quota_gate=quota_gate,
                clock_utc=lambda: current[0],
            )
            intent_id = "intent-quota-expiry"
            client_order_id = stable_client_order_id(
                "BYBIT",
                intent_id,
                environment="PAPER",
                account_id="account-1",
                max_length=36,
                client_id_format="TOKEN",
            )
            prepared = prepare_order_submission(
                capability=capability,
                at=_NOW,
                provider_environment="TESTNET",
                product_family="LINEAR_DERIVATIVES",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity="0.001",
                client_order_id=client_order_id,
                time_in_force="GTC",
                price="50000",
                reduce_only=False,
                position_side="LONG",
                position_idx=1,
            )
            self._mark_ready(runtime)
            dispatch_now = _NOW.isoformat().replace("+00:00", "Z")

            outcome = sender.dispatch(
                attempt_id="attempt-quota-expiry",
                intent_id=intent_id,
                intent_hash="sha256:" + "3" * 64,
                request=guarded_order_projection(prepared),
                now=dispatch_now,
                authority_check=lambda *_args: (True, "allowed"),
                final_barrier_clock=lambda: dispatch_now,
                submission_scope={
                    "endpoint": prepared.endpoint,
                    "prepared_request_sha256": guarded_order_request_sha256(prepared),
                    "capability_snapshot_ids": list(prepared.capability_snapshot_ids),
                    "instrument_versions": list(prepared.instrument_versions),
                    "provider_environment": prepared.provider_environment,
                },
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outcome.reason, "transport_failed_before_send")
            self.assertEqual(quota_calls, 1)
            self.assertEqual(len(wire.requests), 0)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in runtime.journal.load_events_by_aggregate_type(
                        "submission_attempt"
                    )
                ],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_host_revoke_during_quota_wait_is_zero_wire(self) -> None:
        with TemporaryDirectory() as root:
            runtime, host, _boundary = self._runtime(root)
            capability = write_capability(
                family="LINEAR_DERIVATIVES",
                position_mode="HEDGE",
                account_id="account-1",
                environment="PAPER",
                instrument_version="BTCUSDT@1",
                permission_scope="BYBIT.LINEAR.ORDER.WRITE",
                additional_permission_scopes=("ORDER_WRITE",),
                provider_environment="TESTNET",
                expires_at=_NOW + timedelta(minutes=5),
            )
            registry = CapabilityRegistry()
            registry.add(capability)
            quota_calls = 0
            wire = _RecordingWire(
                b'{"retCode":0,"retMsg":"OK","result":{"orderId":"never"}}'
            )

            def quota_gate(*_args):
                nonlocal quota_calls
                quota_calls += 1
                host._serve_state = "CLOSING"

            sender = self._sender(
                runtime,
                capability_registry=registry,
                capability_snapshot_id=capability.snapshot_id,
                wire_client=wire,
                quota_gate=quota_gate,
            )
            intent_id = "intent-quota-revoke"
            client_order_id = stable_client_order_id(
                "BYBIT",
                intent_id,
                environment="PAPER",
                account_id="account-1",
                max_length=36,
                client_id_format="TOKEN",
            )
            prepared = prepare_order_submission(
                capability=capability,
                at=_NOW,
                provider_environment="TESTNET",
                product_family="LINEAR_DERIVATIVES",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity="0.001",
                client_order_id=client_order_id,
                time_in_force="GTC",
                price="50000",
                reduce_only=False,
                position_side="LONG",
                position_idx=1,
            )
            self._mark_ready(runtime)
            dispatch_now = _NOW.isoformat().replace("+00:00", "Z")

            outcome = sender.dispatch(
                attempt_id="attempt-quota-revoke",
                intent_id=intent_id,
                intent_hash="sha256:" + "4" * 64,
                request=guarded_order_projection(prepared),
                now=dispatch_now,
                authority_check=lambda *_args: (True, "allowed"),
                final_barrier_clock=lambda: dispatch_now,
                submission_scope={
                    "endpoint": prepared.endpoint,
                    "prepared_request_sha256": guarded_order_request_sha256(prepared),
                    "capability_snapshot_ids": list(prepared.capability_snapshot_ids),
                    "instrument_versions": list(prepared.instrument_versions),
                    "provider_environment": prepared.provider_environment,
                },
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outcome.reason, "transport_failed_before_send")
            self.assertEqual(quota_calls, 1)
            self.assertEqual(len(wire.requests), 0)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in runtime.journal.load_events_by_aggregate_type(
                        "submission_attempt"
                    )
                ],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_end_to_end_send_uses_one_host_boundary_one_wire_and_durable_terminal(self) -> None:
        credential = json.dumps(
            {"api_key": "api-key-SECRET", "api_secret": "signing-SECRET"},
            sort_keys=True,
            separators=(",", ":"),
        )
        raw_response = (
            b'{"retCode":0,"retMsg":"OK","result":{"orderId":"provider-1"}}'
        )
        with TemporaryDirectory() as root:
            boundary = SecurityBoundary(
                allowed_origins={"http://127.0.0.1:18765"},
                credential_vault=ProtectedCredentialVault(
                    Path(root) / "credentials.json", protector=DeterministicProtector(),
                ),
                session_authorizer=lambda _subject, _role, _origin: True,
                now=lambda: 1000.0,
            )
            runtime, _host, _boundary = self._runtime(root, security_boundary=boundary)
            session = boundary.create_session(
                subject="test-owner", role="OWNER", origin=runtime.config.public_origin,
            )
            credential_handle = boundary.register_secret(
                session.token, origin=runtime.config.public_origin,
                owner_identity=runtime.financial_dispatcher.owner.owner_id,
                account_id="account-1", provider="BYBIT", environment="PAPER",
                provider_environment="TESTNET", purpose="TRADE", secret_value=credential,
            )
            capability = write_capability(
                family="LINEAR_DERIVATIVES",
                position_mode="HEDGE",
                account_id="account-1",
                environment="PAPER",
                instrument_version="BTCUSDT@1",
                permission_scope="BYBIT.LINEAR.ORDER.WRITE",
                additional_permission_scopes=("ORDER_WRITE",),
                provider_environment="TESTNET",
                expires_at=_NOW + timedelta(minutes=5),
            )
            registry = CapabilityRegistry()
            registry.add(capability)
            intent_id = "intent-e2e"
            client_order_id = stable_client_order_id(
                "BYBIT",
                intent_id,
                environment="PAPER",
                account_id="account-1",
                max_length=36,
                client_id_format="TOKEN",
            )
            prepared = prepare_order_submission(
                capability=capability,
                at=_NOW,
                provider_environment="TESTNET",
                product_family="LINEAR_DERIVATIVES",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity="0.001",
                client_order_id=client_order_id,
                time_in_force="GTC",
                price="50000",
                reduce_only=False,
                position_side="LONG",
                position_idx=1,
            )
            wire = _RecordingWire(raw_response)
            sender = self._sender(
                runtime,
                capability_registry=registry,
                capability_snapshot_id=capability.snapshot_id,
                wire_client=wire,
                credential_handle=credential_handle,
                session_token=session.token,
            )
            self._mark_ready(runtime)
            authority_calls = []

            def authority_check(intent_hash: str, at: str):
                authority_calls.append((intent_hash, at))
                return True, "authorized"

            dispatch_now = _NOW.isoformat().replace("+00:00", "Z")
            prepared_request_sha256 = guarded_order_request_sha256(prepared)
            submission_scope = {
                "endpoint": prepared.endpoint,
                "prepared_request_sha256": prepared_request_sha256,
                "capability_snapshot_ids": list(prepared.capability_snapshot_ids),
                "instrument_versions": list(prepared.instrument_versions),
                "provider_environment": prepared.provider_environment,
            }
            outcome = sender.dispatch(
                attempt_id="attempt-e2e", intent_id=intent_id,
                intent_hash="sha256:" + "2" * 64,
                request=guarded_order_projection(prepared), now=dispatch_now,
                authority_check=authority_check,
                final_barrier_clock=lambda: dispatch_now,
                submission_scope=submission_scope,
            )

            self.assertEqual(outcome.status, "SENT")
            self.assertEqual(outcome.reason, "sent_confirmed")
            self.assertEqual(outcome.response["retCode"], 0)
            self.assertEqual(outcome.response["result"]["orderId"], "provider-1")
            self.assertEqual(len(wire.requests), 1)
            self.assertEqual(len(authority_calls), 2)
            events = runtime.journal.load_events_by_aggregate_type("submission_attempt")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )
            self.assertEqual(events[-1]["payload"]["response_encoding"], "utf-8-json")
            self.assertEqual(
                events[-1]["payload"]["response_text"],
                raw_response.decode("utf-8"),
            )
            self.assertEqual(
                events[-1]["payload"]["response_sha256"],
                "sha256:" + sha256(raw_response).hexdigest(),
            )

            binding = load_submission_response_binding(
                runtime.journal,
                environment="PAPER",
                account_id="account-1",
                attempt_id="attempt-e2e",
            )
            self.assertEqual(binding.request_hash, prepared_request_sha256)
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="BYBIT",
                endpoint=prepared.endpoint,
                prepared_request_sha256=prepared_request_sha256,
                capability_snapshot_ids=prepared.capability_snapshot_ids,
                instrument_versions=prepared.instrument_versions,
            )
            normalized = parse_submission_response(
                attempt_id="attempt-e2e",
                prepared_request=prepared,
                observation=observation,
            )
            self.assertEqual(normalized["outcome"], "ACKNOWLEDGED")
            self.assertEqual(normalized["provider_order_id"], "provider-1")
            self.assertEqual(normalized["retry_disposition"], "NEVER")
            self.assertEqual(normalized["client_order_id"], client_order_id)
            self.assertNotIn("fill", repr(normalized).lower())

            replay = sender.dispatch(
                attempt_id="attempt-e2e",
                intent_id=intent_id,
                intent_hash="sha256:" + "2" * 64,
                request=guarded_order_projection(prepared),
                now=dispatch_now,
                authority_check=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("terminal replay must not re-authorize")
                ),
                final_barrier_clock=lambda: (_ for _ in ()).throw(
                    AssertionError("terminal replay must not reacquire barrier")
                ),
                submission_scope=submission_scope,
            )
            self.assertEqual(replay.status, "SENT")
            self.assertEqual(replay.client_order_id, client_order_id)
            self.assertEqual(replay.response["result"]["orderId"], "provider-1")
            self.assertEqual(len(wire.requests), 1)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in runtime.journal.load_events_by_aggregate_type(
                        "submission_attempt"
                    )
                ],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionSent"],
            )


if __name__ == "__main__":
    unittest.main()
