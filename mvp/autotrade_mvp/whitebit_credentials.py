"""Windows user-scoped WhiteBIT READ credential binding.

The only supported logical profile is ``whitebit-primary``. The OS generic
credential store holds the API key and signing secret together; neither value
is accepted from argv, written to repository state, or granted trade authority.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import json
import sys


WHITEBIT_PRIMARY_PROFILE = "whitebit-primary"


class WhitebitCredentialError(RuntimeError):
    """Secret-free credential-store error."""


@dataclass(frozen=True)
class WhitebitReadCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    profile: str = WHITEBIT_PRIMARY_PROFILE
    authority: str = "READ_ONLY"


def _target(profile: str = WHITEBIT_PRIMARY_PROFILE) -> str:
    if type(profile) is not str or profile != WHITEBIT_PRIMARY_PROFILE:
        raise ValueError("WhiteBIT credential profile is invalid")
    return "AutoTrade/ProviderRead/v1/whitebit/whitebit-primary"


def _validate_secret(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 2048
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError("WhiteBIT credential value is invalid")
    return value


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _api():
    if sys.platform != "win32":
        raise WhitebitCredentialError("Windows Credential Manager is unavailable")
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
    advapi.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(pointer)
    ]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredDeleteW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    return advapi, Credential, pointer


def store_whitebit_read_credentials(
    *,
    profile: str = WHITEBIT_PRIMARY_PROFILE,
    api_key: str,
    api_secret: str,
) -> str:
    target = _target(profile)
    document = {
        "api_key": _validate_secret(api_key),
        "api_secret": _validate_secret(api_secret),
        "authority": "READ_ONLY",
        "schema_version": "1.0.0",
    }
    encoded = bytearray(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if len(encoded) > 5000:
        raise ValueError("WhiteBIT credential value is invalid")
    api, Credential, _ = _api()
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer(encoded)
    try:
        credential = Credential()
        credential.type = 1  # CRED_TYPE_GENERIC
        credential.target_name = target
        credential.comment = "AutoTrade WhiteBIT account balance READ only"
        credential.blob_size = len(encoded)
        credential.blob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.persist = 2  # CRED_PERSIST_LOCAL_MACHINE; still user scoped
        credential.user_name = "AutoTrade WhiteBIT READ"
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise WhitebitCredentialError("WhiteBIT credential could not be stored")
        return target
    finally:
        for index in range(len(encoded)):
            encoded[index] = 0


def resolve_whitebit_read_credentials(
    *, profile: str = WHITEBIT_PRIMARY_PROFILE
) -> WhitebitReadCredentials:
    target = _target(profile)
    api, _, pointer = _api()
    credential = pointer()
    if not api.CredReadW(target, 1, 0, ctypes.byref(credential)):
        raise WhitebitCredentialError("WhiteBIT credential is unavailable")
    try:
        if not credential:
            raise WhitebitCredentialError("WhiteBIT credential is invalid")
        size = credential.contents.blob_size
        if size < 2 or size > 5000 or not credential.contents.blob:
            raise WhitebitCredentialError("WhiteBIT credential is invalid")
        raw = ctypes.string_at(credential.contents.blob, size)
        try:
            document = json.loads(raw, object_pairs_hook=_strict_object)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise WhitebitCredentialError("WhiteBIT credential is invalid") from None
        if (
            type(document) is not dict
            or set(document) != {"api_key", "api_secret", "authority", "schema_version"}
            or document["authority"] != "READ_ONLY"
            or document["schema_version"] != "1.0.0"
        ):
            raise WhitebitCredentialError("WhiteBIT credential is invalid")
        try:
            api_key = _validate_secret(document["api_key"])
            api_secret = _validate_secret(document["api_secret"])
        except ValueError:
            raise WhitebitCredentialError("WhiteBIT credential is invalid") from None
        return WhitebitReadCredentials(
            api_key=api_key, api_secret=api_secret, profile=profile
        )
    finally:
        api.CredFree(credential)


def delete_whitebit_read_credentials(
    *, profile: str = WHITEBIT_PRIMARY_PROFILE
) -> None:
    target = _target(profile)
    api, _, _ = _api()
    if not api.CredDeleteW(target, 1, 0):
        raise WhitebitCredentialError("WhiteBIT credential could not be removed")
