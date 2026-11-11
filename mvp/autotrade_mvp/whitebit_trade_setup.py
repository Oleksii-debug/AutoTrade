"""Protected, non-command-line setup for a least-privilege WhiteBIT TRADE key."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

from .whitebit import WhiteBitCredentialBoundary
from .windows_secrets import PersistentCredentialHandle, ProtectedCredentialVault


_SCHEMA = "autotrade-whitebit-trade-binding-v1"
DEFAULT_WHITEBIT_TRADE_BINDING_FILE = "whitebit-live-trade-binding.json"
DEFAULT_WHITEBIT_TRADE_HANDLE_ID = "whitebit-live-trade-v1"


class WhiteBitTradeSetupError(ValueError):
    """The protected TRADE credential binding is absent or inconsistent."""


def _utc_text(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError("observed_at must be an aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _secret(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise WhiteBitTradeSetupError(f"{name} must be exact non-empty text")
    if "\n" in value or "\r" in value or len(value) > 4096:
        raise WhiteBitTradeSetupError(f"{name} is invalid")
    return value


def _binding_path(data_dir: str | Path) -> Path:
    root = Path(data_dir).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise WhiteBitTradeSetupError("AutoTrade data directory is unavailable")
    return root / DEFAULT_WHITEBIT_TRADE_BINDING_FILE


def _write_atomic(path: Path, value: Mapping[str, object]) -> None:
    encoded = json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_whitebit_trade_binding(
    vault: ProtectedCredentialVault,
    *,
    data_dir: str | Path,
    api_key: str,
    api_secret: str,
    permissions: frozenset[str],
    ip_whitelist_enabled: bool,
    observed_at: datetime,
    evidence_ref: str,
    account_id: str = "whitebit-primary",
    owner_identity: str = "local-owner",
) -> tuple[PersistentCredentialHandle, WhiteBitCredentialBoundary]:
    """Install once; secrets enter only the OS-protected vault.

    Permission and IP evidence is checked before the vault is mutated.  The
    adjacent JSON file contains only handle metadata and the operator evidence
    statement needed to revalidate the binding on restart.
    """

    if type(vault) is not ProtectedCredentialVault:
        raise TypeError("vault must be exact ProtectedCredentialVault")
    path = _binding_path(data_dir)
    if path.exists():
        raise WhiteBitTradeSetupError(
            "WhiteBIT TRADE binding already exists; rotate through the fenced workflow"
        )
    normalized_permissions = frozenset(
        item.strip().upper() if type(item) is str else ""
        for item in permissions
    )
    point_text = _utc_text(observed_at)
    # Validate the exact intended authority before persisting any secret.
    provisional = WhiteBitCredentialBoundary(
        credential_binding_id=DEFAULT_WHITEBIT_TRADE_HANDLE_ID,
        credential_generation=1,
        account_id=account_id,
        permissions=normalized_permissions,
        ip_whitelist_enabled=ip_whitelist_enabled,
        environment="LIVE",
        observed_at=observed_at,
        evidence_ref=evidence_ref,
    ).assert_autotrade_safe()
    plaintext = json.dumps(
        {
            "api_key": _secret(api_key, name="api_key"),
            "api_secret": _secret(api_secret, name="api_secret"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    handle = vault.register(
        owner_identity=owner_identity,
        account_id=account_id,
        provider="WHITEBIT",
        environment="LIVE",
        provider_environment="LIVE",
        purpose="TRADE",
        secret_value=plaintext,
        handle_id=DEFAULT_WHITEBIT_TRADE_HANDLE_ID,
    )
    boundary = WhiteBitCredentialBoundary(
        credential_binding_id=handle.handle_id,
        credential_generation=handle.generation,
        account_id=handle.account_id,
        permissions=provisional.permissions,
        ip_whitelist_enabled=provisional.ip_whitelist_enabled,
        environment=handle.environment,
        observed_at=observed_at,
        evidence_ref=provisional.evidence_ref,
    ).assert_autotrade_safe()
    document = {
        "schema": _SCHEMA,
        "handle": asdict(handle),
        "permissions": sorted(boundary.permissions),
        "ip_whitelist_enabled": boundary.ip_whitelist_enabled,
        "observed_at": point_text,
        "evidence_ref": boundary.evidence_ref,
        "contains_secrets": False,
    }
    try:
        _write_atomic(path, document)
    except BaseException:
        vault.revoke(handle, execution_identity=owner_identity)
        raise
    return handle, boundary


def load_whitebit_trade_binding(
    vault: ProtectedCredentialVault,
    *,
    data_dir: str | Path,
) -> tuple[PersistentCredentialHandle, WhiteBitCredentialBoundary]:
    """Load and cross-check non-secret metadata against the protected vault."""

    if type(vault) is not ProtectedCredentialVault:
        raise TypeError("vault must be exact ProtectedCredentialVault")
    path = _binding_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WhiteBitTradeSetupError(
            "WhiteBIT TRADE binding is unavailable"
        ) from error
    if type(raw) is not dict or set(raw) != {
        "schema", "handle", "permissions", "ip_whitelist_enabled",
        "observed_at", "evidence_ref", "contains_secrets",
    }:
        raise WhiteBitTradeSetupError("WhiteBIT TRADE binding fields differ")
    if raw["schema"] != _SCHEMA or raw["contains_secrets"] is not False:
        raise WhiteBitTradeSetupError("WhiteBIT TRADE binding boundary differs")
    metadata = raw["handle"]
    if type(metadata) is not dict or set(metadata) != {
        "handle_id", "account_id", "provider", "environment",
        "provider_environment", "purpose", "generation",
    }:
        raise WhiteBitTradeSetupError("WhiteBIT TRADE handle metadata differs")
    try:
        handle = PersistentCredentialHandle(**metadata)
        observed = datetime.fromisoformat(
            str(raw["observed_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        permissions = raw["permissions"]
        if type(permissions) is not list or any(type(item) is not str for item in permissions):
            raise TypeError("permissions")
        boundary = WhiteBitCredentialBoundary(
            credential_binding_id=handle.handle_id,
            credential_generation=handle.generation,
            account_id=handle.account_id,
            permissions=frozenset(permissions),
            ip_whitelist_enabled=raw["ip_whitelist_enabled"],
            environment=handle.environment,
            observed_at=observed,
            evidence_ref=raw["evidence_ref"],
        ).assert_autotrade_safe()
        current = vault.describe(handle.handle_id)
    except (KeyError, PermissionError, TypeError, ValueError) as error:
        raise WhiteBitTradeSetupError(
            "WhiteBIT TRADE binding is invalid or inactive"
        ) from error
    if current != asdict(handle):
        raise WhiteBitTradeSetupError(
            "WhiteBIT TRADE binding differs from protected credential generation"
        )
    return handle, boundary


__all__ = [
    "DEFAULT_WHITEBIT_TRADE_BINDING_FILE",
    "DEFAULT_WHITEBIT_TRADE_HANDLE_ID",
    "WhiteBitTradeSetupError",
    "install_whitebit_trade_binding",
    "load_whitebit_trade_binding",
]
