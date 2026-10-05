from pathlib import Path


production_path = Path("mvp/autotrade_mvp/production_bybit.py")
source = production_path.read_text(encoding="utf-8")
marker = "def build_production_bybit_order_sender(\n"
assert source.count(marker) == 1
prefix, _marker, _tail = source.partition(marker)
new_tail = '''def build_production_bybit_order_sender(
    runtime: FinancialProductionHostRuntime,
    *,
    financial_issuer: "FinancialSendAuthorityIssuer",
    financial_binding_registry: "DurableFinancialRequestBindingRegistry",
    provider_environment: str,
    capability_snapshot_id: str,
    capability_registry: CapabilityRegistry,
    credential_handle: PersistentCredentialHandle,
    session_token: str,
    clock_millis: ClockMillis,
    clock_utc: ClockUtc,
    quota_gate: QuotaGate | None = None,
    wire_client: ProviderWireClient | None = None,
    recv_window_ms: int = 5000,
) -> "DurableFinanciallyBoundBybitOrderSender":
    """Compose the product Bybit sender under persisted financial authority.

    Product composition cannot obtain the callback-taking raw sender or supply a
    detached financial capability through this public factory.  Every returned
    sender resolves the durable admitted-request binding immediately before the
    existing issuer mint and exact financially-bound provider dispatch.
    """

    from .durable_financial_bybit_sender import (
        bind_durable_financial_bybit_order_sender,
    )
    from .durable_financial_request_binding import (
        DurableFinancialRequestBindingRegistry,
    )
    from .financial_send_authority import (
        FinancialSendAuthorityIssuer,
        bind_financial_bybit_order_sender,
    )

    if type(financial_issuer) is not FinancialSendAuthorityIssuer:
        raise TypeError("financial_issuer must be exact FinancialSendAuthorityIssuer")
    if type(financial_binding_registry) is not DurableFinancialRequestBindingRegistry:
        raise TypeError(
            "financial_binding_registry must be exact DurableFinancialRequestBindingRegistry"
        )
    if financial_issuer.runtime is not runtime:
        raise PermissionError(
            "financial issuer and Bybit sender must share one production host"
        )
    sender = _build_production_bybit_order_sender(
        runtime,
        provider_environment=provider_environment,
        capability_snapshot_id=capability_snapshot_id,
        capability_registry=capability_registry,
        credential_handle=credential_handle,
        session_token=session_token,
        clock_millis=clock_millis,
        clock_utc=clock_utc,
        quota_gate=quota_gate,
        wire_client=wire_client,
        recv_window_ms=recv_window_ms,
    )
    financially_bound = bind_financial_bybit_order_sender(sender, financial_issuer)
    return bind_durable_financial_bybit_order_sender(
        financially_bound,
        financial_issuer,
        financial_binding_registry,
    )
'''
production_path.write_text(prefix + new_tail, encoding="utf-8")


test_path = Path("mvp/tests/test_production_bybit.py")
test = test_path.read_text(encoding="utf-8")
import_anchor = "from mvp.autotrade_mvp.dispatch import stable_client_order_id\n"
assert test.count(import_anchor) == 1
test = test.replace(
    import_anchor,
    import_anchor
    + "from mvp.autotrade_mvp.durable_financial_bybit_sender import (\n"
    + "    DurableFinanciallyBoundBybitOrderSender,\n"
    + ")\n"
    + "from mvp.autotrade_mvp.durable_financial_request_binding import (\n"
    + "    DurableFinancialRequestBindingRegistry,\n"
    + ")\n",
    1,
)

issuer_call = "                    financial_issuer=issuer,\n"
assert test.count(issuer_call) == 1
test = test.replace(
    issuer_call,
    issuer_call
    + "                    financial_binding_registry=DurableFinancialRequestBindingRegistry(runtime.journal),\n",
    1,
)
object_call = "                    financial_issuer=object(),\n"
assert test.count(object_call) == 1
test = test.replace(
    object_call,
    object_call
    + "                    financial_binding_registry=DurableFinancialRequestBindingRegistry(runtime.journal),\n",
    1,
)

old_assertions = '''        self.assertIn("financial_issuer", public_arguments)\n        self.assertNotIn("authority_check", public_arguments)\n\n        bound_arguments = FinanciallyBoundBybitOrderSender.dispatch.__code__.co_varnames[\n            : (\n                FinanciallyBoundBybitOrderSender.dispatch.__code__.co_argcount\n                + FinanciallyBoundBybitOrderSender.dispatch.__code__.co_kwonlyargcount\n            )\n        ]\n        self.assertIn("authority", bound_arguments)\n        self.assertNotIn("authority_check", bound_arguments)\n        self.assertNotIn("final_barrier_clock", bound_arguments)\n'''
new_assertions = '''        self.assertIn("financial_issuer", public_arguments)\n        self.assertIn("financial_binding_registry", public_arguments)\n        self.assertNotIn("authority", public_arguments)\n        self.assertNotIn("binding", public_arguments)\n        self.assertNotIn("authority_check", public_arguments)\n\n        bound_arguments = (\n            DurableFinanciallyBoundBybitOrderSender.dispatch.__code__.co_varnames[\n                : (\n                    DurableFinanciallyBoundBybitOrderSender.dispatch.__code__.co_argcount\n                    + DurableFinanciallyBoundBybitOrderSender.dispatch.__code__.co_kwonlyargcount\n                )\n            ]\n        )\n        self.assertIn("admission_id", bound_arguments)\n        self.assertIn("action", bound_arguments)\n        self.assertNotIn("authority", bound_arguments)\n        self.assertNotIn("binding", bound_arguments)\n        self.assertNotIn("authority_check", bound_arguments)\n        self.assertNotIn("final_barrier_clock", bound_arguments)\n'''
assert test.count(old_assertions) == 1
test = test.replace(old_assertions, new_assertions, 1)
test_path.write_text(test, encoding="utf-8")
