"""Plan 6 Section 5 offline qualification plane regressions.

No real provider account, credentials, network, PAPER/LIVE, or order send.
Existing production Q/C issuers and reconciliation tests remain authoritative.
"""
from __future__ import annotations

import os
import socket
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from tools.plan6_offline_sections import (
    SECTION_MODULES,
    deny_network,
    install_network_deny,
    offline_section5_capability_status,
    reject_provider_credentials,
)


class Plan6OfflineQualificationPlaneTests(unittest.TestCase):
    def test_harness_reuses_existing_authorities_and_restart_tests(self):
        modules = SECTION_MODULES[5]
        for required in (
            "test_provider_qualification_authority",
            "test_provider_qualification_identity",
            "test_provider_account_cut",
            "test_durable_capabilities",
            "test_signed_http_request_envelope",
            "test_authenticated_read_http_request",
            "test_reconciliation_negative_resolution_authority",
            "test_recovery_durable_unknown_restart",
            "test_qualification_attestation_authority_ingress",
        ):
            with self.subTest(required=required):
                self.assertIn("mvp.tests." + required, modules)
        self.assertEqual(len(modules), len(set(modules)))

    def test_real_provider_credentials_fail_closed_and_never_echoed(self):
        for name in (
            "BYBIT_API_KEY",
            "KRAKEN_API_SECRET",
            "IBKR_ACCESS_TOKEN",
            "GITHUB_PROVIDER_API_KEY",
            "RUNNER_PRIVATE_KEY",
            "TRADING_PASSWORD",
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    RuntimeError, "PLAN6_OFFLINE_CREDENTIAL_ENV_PRESENT"
                ) as failure:
                    reject_provider_credentials({name: "CANARY_SECRET_VALUE"})
                self.assertNotIn(name, str(failure.exception))
                self.assertNotIn("CANARY_SECRET_VALUE", str(failure.exception))
        reject_provider_credentials({})

    def test_fake_mapping_cannot_steer_credential_inspection(self):
        class Hostile(dict):
            def __iter__(self):
                raise AssertionError("must not run caller-controlled iterator")

        with self.assertRaisesRegex(RuntimeError, "ENVIRONMENT_INVALID"):
            reject_provider_credentials(Hostile())

    def test_external_account_capabilities_remain_explicitly_unactivated(self):
        status = offline_section5_capability_status()
        self.assertEqual(status["account_environment_binding"], "EXTERNAL_ACTIVATION_PENDING")
        self.assertEqual(status["authenticated_provider_transport"], "EXTERNAL_ACTIVATION_PENDING")
        self.assertEqual(status["provider_capability_attestation"], "EXTERNAL_ACTIVATION_PENDING")
        self.assertEqual(status["paper_live_campaign"], "EXTERNAL_ACTIVATION_PENDING")
        self.assertEqual(status["financial_trading_authority"], "NOT_CLAIMED")
        status["paper_live_campaign"] = "READY"
        self.assertEqual(offline_section5_capability_status()["paper_live_campaign"],
                         "EXTERNAL_ACTIVATION_PENDING")

    def test_network_is_denied_during_provider_module_discovery(self):
        with ExitStack() as stack:
            install_network_deny(stack)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                with self.assertRaisesRegex(RuntimeError, "PLAN6_OFFLINE_NETWORK_DENIED"):
                    udp.sendto(b"no-network", ("127.0.0.1", 9))
            with self.assertRaisesRegex(RuntimeError, "PLAN6_OFFLINE_NETWORK_DENIED"):
                socket.getaddrinfo("provider.invalid", 443)

    def test_network_patch_cleans_up_for_following_suites(self):
        sendto = socket.socket.sendto
        getaddrinfo = socket.getaddrinfo
        with ExitStack() as stack:
            install_network_deny(stack)
            self.assertIs(socket.socket.sendto, deny_network)
            self.assertIs(socket.getaddrinfo, deny_network)
        self.assertIs(socket.socket.sendto, sendto)
        self.assertIs(socket.getaddrinfo, getaddrinfo)

    def test_realistic_ci_environ_without_provider_secrets_is_accepted(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}, clear=True):
            reject_provider_credentials(os.environ)


if __name__ == "__main__":
    unittest.main()
