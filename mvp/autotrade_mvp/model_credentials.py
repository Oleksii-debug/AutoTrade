"""Windows user-scoped AI credential binding, separate from trading credentials.

Only the operating system's generic credential store holds the key. The
returned reference is a non-secret target name and grants no trading rights.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import re
import sys

from .model_provider_registry import model_provider_endpoint


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ModelCredentialError(RuntimeError):
    pass


def _target(provider: str, profile: str) -> str:
    # Mistral predates the shared registry and keeps its exact target for
    # backwards compatibility.  Other providers must be present in the closed
    # endpoint registry before they can receive a credential scope.
    if provider != "mistral":
        model_provider_endpoint(provider)
    if type(profile) is not str or not _MODEL_ID.fullmatch(profile):
        raise ValueError("AI credential scope is invalid")
    return f"AutoTrade/AI/v1/{provider}/{profile}"


def _api():
    if sys.platform != "win32":
        raise ModelCredentialError("Windows Credential Manager is unavailable")
    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)

    class Filetime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class Credential(ctypes.Structure):
        _fields_ = [
            ("flags", wintypes.DWORD), ("type", wintypes.DWORD),
            ("target_name", wintypes.LPWSTR), ("comment", wintypes.LPWSTR),
            ("last_written", Filetime), ("blob_size", wintypes.DWORD),
            ("blob", ctypes.POINTER(ctypes.c_ubyte)), ("persist", wintypes.DWORD),
            ("attribute_count", wintypes.DWORD), ("attributes", ctypes.c_void_p),
            ("target_alias", wintypes.LPWSTR), ("user_name", wintypes.LPWSTR),
        ]

    pointer = ctypes.POINTER(Credential)
    advapi.CredWriteW.argtypes = [pointer, wintypes.DWORD]
    advapi.CredWriteW.restype = wintypes.BOOL
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                 wintypes.DWORD, ctypes.POINTER(pointer)]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredDeleteW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    return advapi, Credential, pointer


def store_model_key(*, profile: str, key: str, provider: str = "mistral") -> str:
    target = _target(provider, profile)
    if type(key) is not str or not key or len(key) > 2500 or "\x00" in key:
        raise ValueError("AI credential value is invalid")
    api, Credential, _ = _api()
    encoded = bytearray(key.encode("utf-16-le"))
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer(encoded)
    try:
        credential = Credential()
        credential.type = 1  # CRED_TYPE_GENERIC
        credential.target_name = target
        credential.blob_size = len(encoded)
        credential.blob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.persist = 2  # CRED_PERSIST_LOCAL_MACHINE, user scoped
        credential.user_name = "AutoTrade AI"
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise ModelCredentialError("AI credential could not be stored")
        return target
    finally:
        for index in range(len(encoded)):
            encoded[index] = 0


def resolve_model_key(*, profile: str, provider: str = "mistral") -> str:
    target = _target(provider, profile)
    api, _, pointer = _api()
    credential = pointer()
    if not api.CredReadW(target, 1, 0, ctypes.byref(credential)):
        raise ModelCredentialError("AI credential is unavailable")
    try:
        size = credential.contents.blob_size
        if size < 2 or size > 5000 or size % 2:
            raise ModelCredentialError("AI credential is invalid")
        value = ctypes.string_at(credential.contents.blob, size).decode("utf-16-le")
        if not value or "\x00" in value:
            raise ModelCredentialError("AI credential is invalid")
        return value
    finally:
        api.CredFree(credential)


def delete_model_key(*, profile: str, provider: str = "mistral") -> None:
    target = _target(provider, profile)
    api, _, _ = _api()
    if not api.CredDeleteW(target, 1, 0):
        raise ModelCredentialError("AI credential could not be removed")
