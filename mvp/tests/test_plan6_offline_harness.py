"""Regression tests for the Plan-6 provider-offline socket boundary.

No real endpoints, provider accounts, secrets, or trading operations are used.
"""
from __future__ import annotations

import socket
import unittest
from contextlib import ExitStack
from tempfile import TemporaryFile

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

    def test_plain_send_and_sendall_are_denied_even_without_connect(self) -> None:
        # A socket inherited in a real test process may already be connected.
        # Without explicit send/sendall patching, those methods can still
        # transmit after the offline test guard has blocked connect().
        with ExitStack() as guard:
            install_network_deny(guard)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                for method in ("send", "sendall"):
                    with self.subTest(method=method):
                        with self.assertRaisesRegex(RuntimeError, "PLAN6_OFFLINE_NETWORK_DENIED"):
                            getattr(sock, method)(b"fixture-only")

    def test_socket_sendfile_is_denied_before_kernel_egress(self) -> None:
        # socket.sendfile can use os.sendfile rather than socket.send/sendall.
        # No provider or remote endpoint is involved; use a local file only.
        with TemporaryFile() as fixture:
            fixture.write(b"fixture-only")
            fixture.seek(0)
            with ExitStack() as guard:
                install_network_deny(guard)
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    with self.assertRaisesRegex(RuntimeError, "PLAN6_OFFLINE_NETWORK_DENIED"):
                        sock.sendfile(fixture)

    def test_guard_restores_sendfile(self) -> None:
        original = socket.socket.sendfile
        with ExitStack() as guard:
            install_network_deny(guard)
            self.assertIs(socket.socket.sendfile, deny_network)
        self.assertIs(socket.socket.sendfile, original)

    def test_guard_restores_send_and_sendall(self) -> None:
        before_send = socket.socket.send
        before_sendall = socket.socket.sendall
        with ExitStack() as guard:
            install_network_deny(guard)
            self.assertIs(socket.socket.send, deny_network)
            self.assertIs(socket.socket.sendall, deny_network)
        self.assertIs(socket.socket.send, before_send)
        self.assertIs(socket.socket.sendall, before_sendall)

    def test_guard_restores_socket_methods(self) -> None:
        before = socket.socket.sendto
        with ExitStack() as guard:
            install_network_deny(guard)
            self.assertIs(socket.socket.sendto, deny_network)
        self.assertIs(socket.socket.sendto, before)


if __name__ == "__main__":
    unittest.main()
