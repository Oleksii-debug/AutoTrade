"""Regression tests for the Plan-6 provider-offline socket boundary.

No real endpoints, provider accounts, secrets, or trading operations are used.
"""
from __future__ import annotations

from contextlib import ExitStack
import socket
import unittest

from tools.plan6_offline_sections import deny_network, install_network_deny


class OfflineSocketBoundaryTests(unittest.TestCase):
    def test_unconnected_udp_sendto_is_denied_before_datagram_egress(self) -> None:
        with ExitStack() as guard:
            install_network_deny(guard)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                with self.assertRaisesRegex(RuntimeError, "PLAN6_OFFLINE_NETWORK_DENIED"):
                    udp.sendto(b"fixture-only", ("127.0.0.1", 9))

    def test_udp_sendmsg_is_denied_where_supported(self) -> None:
        if not hasattr(socket.socket, "sendmsg"):
            self.skipTest("platform has no socket.sendmsg")
        with ExitStack() as guard:
            install_network_deny(guard)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                with self.assertRaisesRegex(RuntimeError, "PLAN6_OFFLINE_NETWORK_DENIED"):
                    udp.sendmsg([b"fixture-only"], [], 0, ("127.0.0.1", 9))

    def test_guard_restores_socket_methods(self) -> None:
        before = socket.socket.sendto
        with ExitStack() as guard:
            install_network_deny(guard)
            self.assertIs(socket.socket.sendto, deny_network)
        self.assertIs(socket.socket.sendto, before)


if __name__ == "__main__":
    unittest.main()
