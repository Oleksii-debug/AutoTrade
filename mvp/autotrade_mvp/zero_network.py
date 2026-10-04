"""Python socket/DNS fence promoted from canonical WP-62 qualification.

This guards the synchronous provider-free ZERO loop. It is not an OS sandbox
and does not qualify external processes or third-party extensions.
"""
from contextlib import contextmanager, ExitStack
import socket
from threading import RLock
from unittest.mock import patch

_LOCK = RLock()


@contextmanager
def deny_python_network():
    attempts = []

    def blocked(*_args, **_kwargs):
        attempts.append("network")
        raise RuntimeError("ZERO mode attempted network access")

    with _LOCK, ExitStack() as stack:
        for target, names in (
            (socket.socket, ("connect", "connect_ex", "send", "sendall", "sendto", "sendmsg", "sendfile")),
            (socket, ("create_connection", "getaddrinfo", "getnameinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr")),
        ):
            for name in names:
                if hasattr(target, name):
                    stack.enter_context(patch.object(target, name, blocked))
        yield attempts
        if attempts:
            raise RuntimeError("ZERO mode observed a blocked network attempt")
