"""Network-denied, secret-free qualification of existing Plan-6 components.

This harness creates no provider, account, trading, or reconciliation authority.
It only executes canonical unit tests under a strict outbound-network deny gate.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))

# Name the existing unit suites explicitly: unknown or renamed modules must
# fail closed rather than silently shrink the offline acceptance surface.
SECTION_MODULES = {
    1: (
        "mvp.tests.test_plan6_offline_harness",
        "mvp.tests.test_provider_domain",
        "mvp.tests.test_provider_account_cut",
        "mvp.tests.test_provider_qualification_identity",
        "mvp.tests.test_provider_qualification_authority",
        "mvp.tests.test_provider_qualification_time_ingress",
        "mvp.tests.test_capabilities",
        "mvp.tests.test_durable_capabilities",
        "mvp.tests.test_provider_selection",
        "mvp.tests.test_provider_qualification_authority",
        "mvp.tests.test_provider_environment_credential_scope",
        "mvp.tests.test_provider_route_authority_composition",
        "mvp.tests.test_provider_route_selection_provenance",
        "mvp.tests.test_provider_evidence_identity",
        "mvp.tests.test_provider_evidence_namespace_generation",
    ),
    2: (
        "mvp.tests.test_provider_core",
        "mvp.tests.test_provider_transport",
        "mvp.tests.test_bybit_transport",
        "mvp.tests.test_kraken_spot_stream",
        "mvp.tests.test_ibkr_authenticated_read_transport",
        "mvp.tests.test_provider_evidence_authenticated_snapshot",
        "mvp.tests.test_provider_route_financial_binding",
        "mvp.tests.test_recovery_dispatch_reconciliation_freshness",
        "mvp.tests.test_provider_route_reads",
        "mvp.tests.test_provider_route_dispatch",
        "mvp.tests.test_provider_route_recovery_provenance",
        "mvp.tests.test_provider_route_transitive_authority",
        "mvp.tests.test_provider_route_selected_route_seal",
        "mvp.tests.test_signed_http_request_envelope",
        "mvp.tests.test_authenticated_read_http_request",
        "mvp.tests.test_authenticated_read_binding_text_ingress",
        "mvp.tests.test_dispatch",
        "mvp.tests.test_dispatch_intent_fence",
        "mvp.tests.test_reconciliation",
        "mvp.tests.test_reconciliation_journal",
        "mvp.tests.test_reconciliation_direct_negative_authority",
        "mvp.tests.test_reconciliation_negative_resolution_authority",
        "mvp.tests.test_recovery_durable_unknown_restart",
    ),
    3: (
        "mvp.tests.test_plan6_family_contracts",
        "mvp.tests.test_bybit_v5",
        "mvp.tests.test_bybit_position_margin",
        "mvp.tests.test_bybit_fee_currency_authority",
        "mvp.tests.test_bybit_option_delivery_raw_parser",
        "mvp.tests.test_bybit_option_delivery_read_policy",
        "mvp.tests.test_kraken_spot_adapter",
        "mvp.tests.test_kraken_futures",
        "mvp.tests.test_kraken_spot_stream",
        "mvp.tests.test_whitebit_adapter",
        "mvp.tests.test_binance_spot",
        "mvp.tests.test_binance_usdm",
        "mvp.tests.test_binance_provider_ingress_regression",
        "mvp.tests.test_ibkr_web_adapter",
        "mvp.tests.test_ibkr_provider_response_ingress",
        "mvp.tests.test_alpaca_adapter",
        "mvp.tests.test_alpaca_options",
        "mvp.tests.test_provider_selection",
    ),
}


def deny_network(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("PLAN6_OFFLINE_NETWORK_DENIED: real provider network is prohibited")


def install_network_deny(stack: ExitStack) -> None:
    """Deny outbound socket sends as well as connection attempts before test imports.

    UDP sendto/sendmsg, preconnected TCP send/sendall and kernel sendfile bypass connect. Guarding connect alone would allow
    real provider datagrams while producing a false "network: DENIED" receipt.
    """
    for method in ("connect", "connect_ex", "send", "sendall", "sendto", "sendfile"):
        stack.enter_context(patch.object(socket.socket, method, deny_network))
    if hasattr(socket.socket, "sendmsg"):
        stack.enter_context(patch.object(socket.socket, "sendmsg", deny_network))
    stack.enter_context(patch.object(socket, "create_connection", deny_network))
    # DNS lookups can transmit queries before any guarded socket.connect call.
    # Keep the offline guarantee fail-closed even for resolver-only code paths.
    for resolver in (
        "getaddrinfo", "gethostbyname", "gethostbyname_ex",
        "gethostbyaddr", "getnameinfo", "getfqdn",
    ):
        if hasattr(socket, resolver):
            stack.enter_context(patch.object(socket, resolver, deny_network))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--section", type=int, choices=sorted(SECTION_MODULES), required=True)
    args = parser.parse_args()

    # Credentials must never be imported into test fixtures. The GitHub Actions
    # workflow exposes no secrets and gives only read-only repository access.
    # Fail if a CI environment accidentally supplies provider credential names.
    sensitive = ("API_KEY", "API_SECRET", "ACCESS_TOKEN", "PRIVATE_KEY",
                 "PROVIDER_PASSWORD", "TRADING_PASSWORD")
    leaked_names = sorted(
        key for key in os.environ
        if any(fragment in key.upper() for fragment in sensitive)
        and not key.startswith(("GITHUB_", "ACTIONS_", "RUNNER_"))
    )
    if leaked_names:
        raise RuntimeError(
            "PLAN6_OFFLINE_CREDENTIAL_ENV_PRESENT (names suppressed)"
        )

    modules = tuple(dict.fromkeys(SECTION_MODULES[args.section]))
    # Test discovery imports modules and can run module-level code. Enforce the
    # offline boundary before importing *any* provider or test module; keeping
    # this guard only around TextTestRunner would leave import-time I/O open.
    with ExitStack() as stack:
        install_network_deny(stack)
        suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
        count = suite.countTestCases()
        if not modules or count < len(modules):
            raise RuntimeError("PLAN6_OFFLINE_TEST_DISCOVERY_INCOMPLETE")
        result = unittest.TextTestRunner(verbosity=2).run(suite)

    ok = result.wasSuccessful() and result.testsRun == count
    print(json.dumps({
        "schema_version": "plan6-offline-suite.v1",
        "section": args.section,
        "test_modules": modules,
        "test_count": result.testsRun,
        "status": "PASS" if ok else "FAIL",
        "network": "DENIED",
        "provider_credentials": "ABSENT",
        "provider_account_activation": "NOT_CLAIMED",
        "paper_live": "NOT_CLAIMED",
    }, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
