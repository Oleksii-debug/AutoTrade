"""Per-user protected credential storage for Windows releases.

Financial role/session authorization remains owned by security.py. This module
supplies the storage primitive: exact scoped metadata plus ciphertext protected
for the current Windows identity. It never implements withdrawal or
external-transfer credentials.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Protocol
from uuid import uuid4


class SecretVaultError(ValueError):
    pass


_ALLOWED_PURPOSES = frozenset({"READ", "TRADE"})
_ALLOWED_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})


class SecretProtector(Protocol):
    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes: ...
    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes: ...


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SecretVaultError(f"{name} is required")
    return value.strip()


def _provider_environment(
    value: object | None,
    *,
    fallback_environment: str,
) -> str:
    if value is None:
        return fallback_environment
    normalized = _text(value, name="provider_environment").upper()
    if len(normalized) > 64 or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in normalized
    ):
        raise SecretVaultError("provider_environment is not canonical")
    return normalized


def _scope_entropy(
    *,
    handle_id: str,
    owner_identity: str,
    account_id: str,
    provider: str,
    environment: str,
    provider_environment: str,
    purpose: str,
    generation: int,
) -> bytes:
    payload = {
        "handle_id": handle_id,
        "owner_identity": owner_identity,
        "account_id": account_id,
        "provider": provider,
        "environment": environment,
        "provider_environment": provider_environment,
        "purpose": purpose,
        "generation": generation,
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).digest()


@contextmanager
def _exclusive_file_lock(path: Path):
    """Serialize vault mutations across processes sharing one credential file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class DpapiCurrentUserProtector:
    """Windows DPAPI current-user protector with UI disabled."""

    CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows DPAPI is available only on Windows")

    @staticmethod
    def _crypt(*, data: bytes, entropy: bytes, decrypt: bool) -> bytes:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        def blob(value: bytes):
            if value:
                buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
                return DATA_BLOB(
                    len(value),
                    ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
                ), buffer
            return DATA_BLOB(0, None), None

        crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
        kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        source, source_buffer = blob(data)
        entropy_blob, entropy_buffer = blob(entropy)
        output = DATA_BLOB()

        if decrypt:
            function = crypt32.CryptUnprotectData
            function.argtypes = [
                ctypes.POINTER(DATA_BLOB),
                ctypes.c_void_p,
                ctypes.POINTER(DATA_BLOB),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(DATA_BLOB),
            ]
            function.restype = wintypes.BOOL
            ok = function(
                ctypes.byref(source),
                None,
                ctypes.byref(entropy_blob),
                None,
                None,
                DpapiCurrentUserProtector.CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            )
        else:
            function = crypt32.CryptProtectData
            function.argtypes = [
                ctypes.POINTER(DATA_BLOB),
                wintypes.LPCWSTR,
                ctypes.POINTER(DATA_BLOB),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(DATA_BLOB),
            ]
            function.restype = wintypes.BOOL
            ok = function(
                ctypes.byref(source),
                "AutoTrade credential",
                ctypes.byref(entropy_blob),
                None,
                None,
                DpapiCurrentUserProtector.CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            )

        _ = source_buffer, entropy_buffer
        if not ok:
            raise OSError(ctypes.get_last_error(), "Windows DPAPI operation failed")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            kernel32.LocalFree(output.pbData)

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        if not isinstance(plaintext, bytes) or not plaintext:
            raise SecretVaultError("plaintext must be non-empty bytes")
        if not isinstance(entropy, bytes) or not entropy:
            raise SecretVaultError("entropy must be non-empty bytes")
        return self._crypt(data=plaintext, entropy=entropy, decrypt=False)

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        if not isinstance(ciphertext, bytes) or not ciphertext:
            raise SecretVaultError("ciphertext must be non-empty bytes")
        if not isinstance(entropy, bytes) or not entropy:
            raise SecretVaultError("entropy must be non-empty bytes")
        return self._crypt(data=ciphertext, entropy=entropy, decrypt=True)


@dataclass(frozen=True)
class PersistentCredentialHandle:
    handle_id: str
    account_id: str
    provider: str
    environment: str
    purpose: str
    generation: int
    provider_environment: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "handle_id", _text(self.handle_id, name="handle_id"))
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(self, "provider", _text(self.provider, name="provider").upper())
        environment = _text(self.environment, name="environment").upper()
        if environment not in _ALLOWED_ENVIRONMENTS:
            raise SecretVaultError("credential environment is not canonical")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self,
            "provider_environment",
            _provider_environment(
                self.provider_environment,
                fallback_environment=environment,
            ),
        )
        purpose = _text(self.purpose, name="purpose").upper()
        if purpose not in _ALLOWED_PURPOSES:
            raise SecretVaultError("credential purpose is not allowed")
        object.__setattr__(self, "purpose", purpose)
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 1
        ):
            raise SecretVaultError("credential generation is invalid")


@dataclass(frozen=True)
class CredentialReattachmentRequirement:
    """Non-secret credential metadata that must be explicitly reattached."""

    handle: PersistentCredentialHandle
    was_active: bool

    def __post_init__(self) -> None:
        if not isinstance(self.handle, PersistentCredentialHandle):
            raise TypeError("handle must be PersistentCredentialHandle")
        if type(self.was_active) is not bool:
            raise SecretVaultError("was_active must be boolean")


class ProtectedCredentialVault:
    """Atomic metadata+ciphertext vault using an injected OS protector."""

    FORMAT_VERSION = 3
    ALLOWED_PURPOSES = _ALLOWED_PURPOSES
    ALLOWED_ENVIRONMENTS = _ALLOWED_ENVIRONMENTS

    def __init__(self, path: str | Path, *, protector: SecretProtector) -> None:
        self.path = Path(path)
        if not hasattr(protector, "protect") or not hasattr(protector, "unprotect"):
            raise TypeError("protector must implement protect and unprotect")
        self._protector = protector
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        with _exclusive_file_lock(self.lock_path):
            if not self.path.exists():
                self._write({"version": self.FORMAT_VERSION, "records": {}})
            else:
                self._load()

    def _load(self) -> dict[str, object]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SecretVaultError("credential vault is corrupt or unreadable") from error
        if not isinstance(raw, dict):
            raise SecretVaultError("credential vault version is unsupported")
        version = raw.get("version")
        if version in {1, 2}:
            raise SecretVaultError(
                "legacy credential vault lacks exact provider-environment binding; "
                "explicit credential reattachment is required"
            )
        if version != self.FORMAT_VERSION:
            raise SecretVaultError("credential vault version is unsupported")
        records = raw.get("records")
        if not isinstance(records, dict):
            raise SecretVaultError("credential vault records are invalid")
        for handle_id, record in records.items():
            if not isinstance(handle_id, str) or not isinstance(record, dict):
                raise SecretVaultError("credential vault record is invalid")
            required = {"handle", "owner_identity", "ciphertext", "active"}
            if set(record) != required:
                raise SecretVaultError("credential vault record fields are invalid")
            handle = record["handle"]
            if not isinstance(handle, dict) or set(handle) != {
                "handle_id",
                "account_id",
                "provider",
                "environment",
                "provider_environment",
                "purpose",
                "generation",
            }:
                raise SecretVaultError("credential handle metadata is invalid")
            if handle.get("handle_id") != handle_id:
                raise SecretVaultError("credential handle identity mismatch")
            try:
                parsed_handle = PersistentCredentialHandle(
                    handle_id=handle.get("handle_id"),
                    account_id=handle.get("account_id"),
                    provider=handle.get("provider"),
                    environment=handle.get("environment"),
                    provider_environment=handle.get("provider_environment"),
                    purpose=handle.get("purpose"),
                    generation=handle.get("generation"),
                )
                owner_identity = _text(record["owner_identity"], name="owner_identity")
            except (SecretVaultError, TypeError) as error:
                raise SecretVaultError("credential vault scope metadata is invalid") from error
            if parsed_handle.handle_id != handle_id:
                raise SecretVaultError("credential handle identity mismatch")
            if not isinstance(record["active"], bool):
                raise SecretVaultError("credential active flag is invalid")
            try:
                ciphertext = b64decode(record["ciphertext"], validate=True)
                if not ciphertext:
                    raise ValueError("empty ciphertext")
            except Exception as error:
                raise SecretVaultError("credential ciphertext is invalid") from error
            _ = owner_identity
        return raw

    def _write(self, value: dict[str, object]) -> None:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        fd, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _normalize_scope(
        *,
        owner_identity: object,
        account_id: object,
        provider: object,
        environment: object,
        purpose: object,
        provider_environment: object | None = None,
    ) -> tuple[str, str, str, str, str, str]:
        owner = _text(owner_identity, name="owner_identity")
        account = _text(account_id, name="account_id")
        normalized_provider = _text(provider, name="provider").upper()
        normalized_environment = _text(environment, name="environment").upper()
        if normalized_environment not in ProtectedCredentialVault.ALLOWED_ENVIRONMENTS:
            raise PermissionError("Credential environment is not canonical")
        normalized_provider_environment = _provider_environment(
            provider_environment,
            fallback_environment=normalized_environment,
        )
        normalized_purpose = _text(purpose, name="purpose").upper()
        if normalized_purpose not in ProtectedCredentialVault.ALLOWED_PURPOSES:
            raise PermissionError(
                "Credential purpose is not an allowed read/trade scope"
            )
        return (
            owner,
            account,
            normalized_provider,
            normalized_environment,
            normalized_provider_environment,
            normalized_purpose,
        )

    @staticmethod
    def _handle(record: dict[str, object]) -> PersistentCredentialHandle:
        metadata = record["handle"]
        return PersistentCredentialHandle(
            handle_id=str(metadata["handle_id"]),
            account_id=str(metadata["account_id"]),
            provider=str(metadata["provider"]),
            environment=str(metadata["environment"]),
            provider_environment=str(metadata["provider_environment"]),
            purpose=str(metadata["purpose"]),
            generation=int(metadata["generation"]),
        )

    def register(
        self,
        *,
        owner_identity: str,
        account_id: str,
        provider: str,
        environment: str,
        purpose: str,
        secret_value: str,
        provider_environment: str | None = None,
        handle_id: str | None = None,
    ) -> PersistentCredentialHandle:
        (
            owner,
            account,
            normalized_provider,
            normalized_environment,
            normalized_provider_environment,
            normalized_purpose,
        ) = self._normalize_scope(
            owner_identity=owner_identity,
            account_id=account_id,
            provider=provider,
            environment=environment,
            purpose=purpose,
            provider_environment=provider_environment,
        )
        if not isinstance(secret_value, str) or not secret_value:
            raise SecretVaultError("secret_value must not be empty")
        hid = (
            _text(handle_id, name="handle_id")
            if handle_id is not None
            else "cred_" + uuid4().hex
        )
        with _exclusive_file_lock(self.lock_path):
            state = self._load()
            records = state["records"]
            if hid in records:
                raise SecretVaultError("handle_id already exists")
            generation = 1
            entropy = _scope_entropy(
                handle_id=hid,
                owner_identity=owner,
                account_id=account,
                provider=normalized_provider,
                environment=normalized_environment,
                provider_environment=normalized_provider_environment,
                purpose=normalized_purpose,
                generation=generation,
            )
            ciphertext = self._protector.protect(
                secret_value.encode("utf-8"),
                entropy=entropy,
            )
            handle = PersistentCredentialHandle(
                handle_id=hid,
                account_id=account,
                provider=normalized_provider,
                environment=normalized_environment,
                provider_environment=normalized_provider_environment,
                purpose=normalized_purpose,
                generation=generation,
            )
            records[hid] = {
                "handle": asdict(handle),
                "owner_identity": owner,
                "ciphertext": b64encode(ciphertext).decode("ascii"),
                "active": True,
            }
            self._write(state)
            return handle

    def describe(self, handle_id: str) -> dict[str, object]:
        hid = _text(handle_id, name="handle_id")
        state = self._load()
        record = state["records"].get(hid)
        if record is None or record["active"] is not True:
            raise PermissionError("Credential is unavailable")
        handle = self._handle(record)
        return {
            "handle_id": handle.handle_id,
            "account_id": handle.account_id,
            "provider": handle.provider,
            "environment": handle.environment,
            "provider_environment": handle.provider_environment,
            "purpose": handle.purpose,
            "generation": handle.generation,
        }

    def export_reattachment_manifest(self) -> dict[str, object]:
        """Export only credential metadata; never ciphertext or owner identity.

        This manifest is intentionally not a credential backup.  Restoring it
        cannot recreate an admitted credential: every active handle requires
        explicit secret reattachment and normal capability requalification.
        """

        with _exclusive_file_lock(self.lock_path):
            state = self._load()
            records = state["records"]
            exported = []
            for handle_id in sorted(records):
                record = records[handle_id]
                handle = self._handle(record)
                exported.append(
                    {
                        "handle": asdict(handle),
                        "active": record["active"],
                    }
                )
            return {
                "schema_version": "1.0.0",
                "source_vault_format_version": self.FORMAT_VERSION,
                "contains_secrets": False,
                "restore_mode": "EXPLICIT_REATTACHMENT_REQUIRED",
                "records": exported,
            }

    @staticmethod
    def validate_reattachment_manifest(
        manifest: object,
    ) -> tuple[CredentialReattachmentRequirement, ...]:
        """Validate metadata for an explicit reattachment workflow.

        Validation has no side effects and cannot populate a vault.  Secret
        material must arrive later through the normal privileged registration
        boundary under the current OS identity.
        """

        if not isinstance(manifest, dict) or set(manifest) != {
            "schema_version",
            "source_vault_format_version",
            "contains_secrets",
            "restore_mode",
            "records",
        }:
            raise SecretVaultError(
                "credential reattachment manifest shape is invalid"
            )
        if manifest["schema_version"] != "1.0.0":
            raise SecretVaultError(
                "credential reattachment manifest schema is unsupported"
            )
        if manifest["source_vault_format_version"] != ProtectedCredentialVault.FORMAT_VERSION:
            raise SecretVaultError(
                "credential reattachment manifest vault format is unsupported"
            )
        if manifest["contains_secrets"] is not False:
            raise SecretVaultError(
                "credential reattachment manifest must not contain secrets"
            )
        if manifest["restore_mode"] != "EXPLICIT_REATTACHMENT_REQUIRED":
            raise SecretVaultError(
                "credential reattachment manifest cannot restore authority"
            )
        records = manifest["records"]
        if not isinstance(records, list):
            raise SecretVaultError(
                "credential reattachment records must be a list"
            )

        requirements: list[CredentialReattachmentRequirement] = []
        seen: set[str] = set()
        expected_handle_fields = {
            "handle_id",
            "account_id",
            "provider",
            "environment",
            "provider_environment",
            "purpose",
            "generation",
        }
        for item in records:
            if not isinstance(item, dict) or set(item) != {"handle", "active"}:
                raise SecretVaultError(
                    "credential reattachment record is invalid"
                )
            metadata = item["handle"]
            if not isinstance(metadata, dict) or set(metadata) != expected_handle_fields:
                raise SecretVaultError(
                    "credential reattachment handle metadata is invalid"
                )
            try:
                handle = PersistentCredentialHandle(
                    handle_id=metadata["handle_id"],
                    account_id=metadata["account_id"],
                    provider=metadata["provider"],
                    environment=metadata["environment"],
                    provider_environment=metadata["provider_environment"],
                    purpose=metadata["purpose"],
                    generation=metadata["generation"],
                )
            except (SecretVaultError, TypeError, KeyError) as error:
                raise SecretVaultError(
                    "credential reattachment scope metadata is invalid"
                ) from error
            if handle.handle_id in seen:
                raise SecretVaultError(
                    "credential reattachment handle identity is duplicated"
                )
            seen.add(handle.handle_id)
            active = item["active"]
            if type(active) is not bool:
                raise SecretVaultError(
                    "credential reattachment active flag is invalid"
                )
            requirements.append(
                CredentialReattachmentRequirement(
                    handle=handle,
                    was_active=active,
                )
            )
        return tuple(requirements)

    def _prove_current_identity_can_decrypt(
        self,
        record: dict[str, object],
        handle: PersistentCredentialHandle,
        *,
        owner_identity: str,
    ) -> None:
        entropy = _scope_entropy(
            handle_id=handle.handle_id,
            owner_identity=owner_identity,
            account_id=handle.account_id,
            provider=handle.provider,
            environment=handle.environment,
            provider_environment=handle.provider_environment,
            purpose=handle.purpose,
            generation=handle.generation,
        )
        try:
            plaintext = self._protector.unprotect(
                b64decode(record["ciphertext"], validate=True),
                entropy=entropy,
            )
            if not isinstance(plaintext, bytes) or not plaintext:
                raise OSError("protector returned invalid plaintext")
            plaintext.decode("utf-8")
        except (UnicodeDecodeError, OSError, ValueError, TypeError) as error:
            raise PermissionError(
                "Credential cannot be decrypted in this identity"
            ) from error

    def resolve(
        self,
        handle: PersistentCredentialHandle,
        *,
        execution_identity: str,
        account_id: str,
        provider: str,
        environment: str,
        purpose: str,
        provider_environment: str | None = None,
    ) -> str:
        if not isinstance(handle, PersistentCredentialHandle):
            raise TypeError("handle must be a PersistentCredentialHandle")
        (
            owner,
            account,
            normalized_provider,
            normalized_environment,
            normalized_provider_environment,
            normalized_purpose,
        ) = self._normalize_scope(
            owner_identity=execution_identity,
            account_id=account_id,
            provider=provider,
            environment=environment,
            purpose=purpose,
            provider_environment=provider_environment,
        )
        state = self._load()
        record = state["records"].get(handle.handle_id)
        if record is None or record["active"] is not True:
            raise PermissionError("Credential is unavailable")
        current = self._handle(record)
        if current != handle:
            raise PermissionError("Credential handle generation is stale")
        if record["owner_identity"] != owner:
            raise PermissionError("Execution identity cannot decrypt this credential")
        if (
            current.account_id != account
            or current.provider != normalized_provider
            or current.environment != normalized_environment
            or current.provider_environment != normalized_provider_environment
        ):
            raise PermissionError("Credential scope mismatch")
        if current.purpose != normalized_purpose:
            raise PermissionError("Credential purpose mismatch")
        entropy = _scope_entropy(
            handle_id=current.handle_id,
            owner_identity=owner,
            account_id=current.account_id,
            provider=current.provider,
            environment=current.environment,
            provider_environment=current.provider_environment,
            purpose=current.purpose,
            generation=current.generation,
        )
        try:
            plaintext = self._protector.unprotect(
                b64decode(record["ciphertext"], validate=True),
                entropy=entropy,
            )
            if not isinstance(plaintext, bytes) or not plaintext:
                raise OSError("protector returned invalid plaintext")
            return plaintext.decode("utf-8")
        except (UnicodeDecodeError, OSError, ValueError, TypeError) as error:
            raise PermissionError(
                "Credential cannot be decrypted in this identity"
            ) from error

    def rotate(
        self,
        handle: PersistentCredentialHandle,
        *,
        execution_identity: str,
        new_secret_value: str,
    ) -> PersistentCredentialHandle:
        if not isinstance(handle, PersistentCredentialHandle):
            raise TypeError("handle must be a PersistentCredentialHandle")
        if not isinstance(new_secret_value, str) or not new_secret_value:
            raise SecretVaultError("new_secret_value must not be empty")
        owner = _text(execution_identity, name="execution_identity")
        with _exclusive_file_lock(self.lock_path):
            state = self._load()
            record = state["records"].get(handle.handle_id)
            if record is None or record["active"] is not True:
                raise PermissionError("Credential is unavailable")
            current = self._handle(record)
            if current != handle:
                raise PermissionError("Credential handle generation is stale")
            if record["owner_identity"] != owner:
                raise PermissionError("Secret identity mismatch")
            # The metadata identity is only a scope label.  Rotation must also
            # prove that the current OS/protector identity can decrypt the
            # existing generation; otherwise a copied vault could be silently
            # rebound to a different Windows user that supplies the same label.
            self._prove_current_identity_can_decrypt(
                record,
                current,
                owner_identity=owner,
            )
            next_handle = PersistentCredentialHandle(
                handle_id=current.handle_id,
                account_id=current.account_id,
                provider=current.provider,
                environment=current.environment,
                provider_environment=current.provider_environment,
                purpose=current.purpose,
                generation=current.generation + 1,
            )
            entropy = _scope_entropy(
                handle_id=next_handle.handle_id,
                owner_identity=owner,
                account_id=next_handle.account_id,
                provider=next_handle.provider,
                environment=next_handle.environment,
                provider_environment=next_handle.provider_environment,
                purpose=next_handle.purpose,
                generation=next_handle.generation,
            )
            record["handle"] = asdict(next_handle)
            record["ciphertext"] = b64encode(
                self._protector.protect(
                    new_secret_value.encode("utf-8"),
                    entropy=entropy,
                )
            ).decode("ascii")
            self._write(state)
            return next_handle

    def revoke(
        self,
        handle: PersistentCredentialHandle,
        *,
        execution_identity: str,
    ) -> None:
        if not isinstance(handle, PersistentCredentialHandle):
            raise TypeError("handle must be a PersistentCredentialHandle")
        owner = _text(execution_identity, name="execution_identity")
        with _exclusive_file_lock(self.lock_path):
            state = self._load()
            record = state["records"].get(handle.handle_id)
            if record is None or record["active"] is not True:
                raise PermissionError("Credential is unavailable")
            current = self._handle(record)
            if current != handle:
                raise PermissionError("Credential handle generation is stale")
            if record["owner_identity"] != owner:
                raise PermissionError("Secret identity mismatch")
            self._prove_current_identity_can_decrypt(
                record,
                current,
                owner_identity=owner,
            )
            record["active"] = False
            record["ciphertext"] = b64encode(os.urandom(32)).decode("ascii")
            self._write(state)
