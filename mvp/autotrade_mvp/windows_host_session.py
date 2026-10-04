"""Current-user Windows handoff for the local ZERO owner session.

The canonical session authority remains SecurityBoundary. This module only
persists the already-issued short-lived bearer in the exact Windows Credential
Manager shape consumed by AutoTrade.Desktop; it never mints or broadens roles.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from urllib.parse import urlsplit


_TARGET_PREFIX = "AutoTrade.HostSession:"
_CREDENTIAL_TYPE_GENERIC = 1
_CREDENTIAL_PERSIST_SESSION = 1
_MAX_BLOB_BYTES = 8192


def desktop_session_credential_target(origin: str) -> str:
    """Return the exact target consumed by the native ZERO safety shell."""

    if type(origin) is not str or not origin or origin != origin.strip():
        raise ValueError("desktop session origin must be canonical text")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != ""
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("desktop session origin must be exact loopback HTTP origin")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("desktop session origin port is invalid") from error
    if port is None or not (1 <= port <= 65535):
        raise ValueError("desktop session origin requires an explicit port")
    canonical = f"http://127.0.0.1:{port}"
    if origin != canonical:
        raise ValueError("desktop session origin is not canonical")
    return _TARGET_PREFIX + canonical


def _running_on_windows() -> bool:
    return os.name == "nt"


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _write_windows_generic_credential(*, target: str, actor: str, token: str) -> None:
    if not _running_on_windows():
        raise OSError("Windows Credential Manager is available only on Windows")

    blob = bytearray(token.encode("utf-16-le"))
    if not blob or len(blob) > _MAX_BLOB_BYTES:
        raise ValueError("desktop session token is too large for Credential Manager")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer(blob)
    credential = _CREDENTIALW(
        Flags=0,
        Type=_CREDENTIAL_TYPE_GENERIC,
        TargetName=target,
        Comment="AutoTrade local ZERO owner session",
        LastWritten=wintypes.FILETIME(),
        CredentialBlobSize=len(blob),
        CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        Persist=_CREDENTIAL_PERSIST_SESSION,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName=actor,
    )
    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    write = advapi32.CredWriteW
    write.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    write.restype = wintypes.BOOL
    try:
        if not write(ctypes.byref(credential), 0):
            raise OSError(
                ctypes.get_last_error(),
                "Windows Credential Manager session handoff failed",
            )
    finally:
        blob[:] = b"\x00" * len(blob)


def persist_desktop_owner_session(*, origin: str, actor: str, token: str) -> str:
    """Persist one already-issued owner token for the same current Windows user."""

    target = desktop_session_credential_target(origin)
    if type(actor) is not str or not actor or actor != actor.strip():
        raise ValueError("desktop session actor must be canonical text")
    if type(token) is not str or not token or token != token.strip() or "\x00" in token:
        raise ValueError("desktop session token must be canonical text")
    if _running_on_windows():
        _write_windows_generic_credential(
            target=target,
            actor=actor,
            token=token,
        )
    return target
