"""Vault-issued non-secret TRADE credential transition receipts for WP-49.

This module deliberately reuses the exact ``ProtectedCredentialVault`` file,
protector and inter-process lock. It does not create a second secret store,
recovery controller, sender lease, or terminal takeover authority.

Receipts are useful only as one controlled-transfer input. Recovery must still
prove that the exact old process incarnation exited before the credential
transition and must keep UNKNOWN/provider reconciliation independent.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from time import time_ns
from uuid import uuid4

from .provider_domain import ProviderDomainError, normalize_provider_environment
from .windows_secrets import (
    PersistentCredentialHandle,
    ProtectedCredentialVault,
    SecretVaultError,
    _exclusive_file_lock,
    _scope_entropy,
    _text,
)


_AUTHORITY_KEY = "credential_transition_authority"
_SCHEMA_VERSION = "2.0.0"
_RECEIPT_FIELDS = {
    "schema_version",
    "receipt_id",
    "operation",
    "handle_id",
    "account_id",
    "provider",
    "environment",
    "provider_environment",
    "purpose",
    "prior_generation",
    "successor_generation",
    "active_after",
    "owner_identity_sha256",
    "vault_authority_sha256",
    "record_state_sha256",
    "previous_receipt_id",
    "transition_sequence",
    "completed_time_ns",
}


class CredentialTransitionReceiptError(PermissionError):
    """Raised when a transition receipt is not current vault-issued evidence."""


# Retain installed transition/integrity primitives once. Later module/class
# rebinding must not retarget the already-selected credential authority path.
_CANONICAL_EXCLUSIVE_FILE_LOCK = _exclusive_file_lock
_CANONICAL_VAULT_TYPE = ProtectedCredentialVault
_CANONICAL_HANDLE_TYPE = PersistentCredentialHandle
_CANONICAL_VAULT_LOAD = ProtectedCredentialVault._load
_CANONICAL_VAULT_HANDLE = ProtectedCredentialVault._handle
_CANONICAL_VAULT_PROVE_CURRENT_IDENTITY = (
    ProtectedCredentialVault._prove_current_identity_can_decrypt
)
_CANONICAL_VAULT_WRITE = ProtectedCredentialVault._write
_CANONICAL_SCOPE_ENTROPY = _scope_entropy
_CANONICAL_TEXT = _text
_CANONICAL_JSON_DUMPS = json.dumps
_CANONICAL_SHA256 = sha256
_CANONICAL_ASDICT = asdict
_CANONICAL_B64DECODE = b64decode
_CANONICAL_B64ENCODE = b64encode
_CANONICAL_UUID4 = uuid4
_CANONICAL_TIME_NS = time_ns
_CANONICAL_URANDOM = os.urandom
_CANONICAL_NORMALIZE_PROVIDER_ENVIRONMENT = normalize_provider_environment


def _canonical_bytes(
    value: object,
    *,
    _json_dumps=_CANONICAL_JSON_DUMPS,
) -> bytes:
    return _json_dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: bytes, *, _sha256=_CANONICAL_SHA256) -> str:
    return "sha256:" + _sha256(value).hexdigest()


def _text_digest(value: str, *, _digest_fn=_digest) -> str:
    return _digest_fn(value.encode("utf-8"))


@dataclass(frozen=True)
class CredentialTransitionReceipt:
    """Detached public metadata for one exact committed TRADE transition.

    The receipt contains no secret/ciphertext/raw owner identity. It is not
    self-authenticating: ``verify_trade_credential_transition_receipt`` must
    re-open the selected canonical vault and validate the internally retained
    protector seal plus exact current credential state.
    """

    schema_version: str
    receipt_id: str
    operation: str
    handle_id: str
    account_id: str
    provider: str
    environment: str
    provider_environment: str
    purpose: str
    prior_generation: int
    successor_generation: int | None
    active_after: bool
    owner_identity_sha256: str
    vault_authority_sha256: str
    record_state_sha256: str
    previous_receipt_id: str | None
    transition_sequence: int
    completed_time_ns: int

    def __post_init__(
        self,
        _normalize_provider_environment=_CANONICAL_NORMALIZE_PROVIDER_ENVIRONMENT,
    ) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise CredentialTransitionReceiptError(
                "credential transition receipt schema is unsupported"
            )
        for name in (
            "handle_id",
            "account_id",
            "provider",
            "environment",
            "provider_environment",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise CredentialTransitionReceiptError(
                    f"credential transition receipt {name} is invalid"
                )
        if self.provider != self.provider.upper():
            raise CredentialTransitionReceiptError(
                "credential transition receipt provider is not canonical"
            )
        if self.environment != self.environment.upper():
            raise CredentialTransitionReceiptError(
                "credential transition receipt environment is not canonical"
            )
        try:
            normalized_provider_environment = _normalize_provider_environment(
                provider_id=self.provider,
                environment=self.environment,
                provider_environment=self.provider_environment,
            )
        except ProviderDomainError as error:
            raise CredentialTransitionReceiptError(
                "credential transition receipt provider_environment is invalid"
            ) from error
        if normalized_provider_environment != self.provider_environment:
            raise CredentialTransitionReceiptError(
                "credential transition receipt provider_environment is not canonical"
            )
        if self.purpose != "TRADE":
            raise CredentialTransitionReceiptError(
                "credential transition receipt must bind TRADE purpose"
            )
        if self.operation not in {"ROTATED", "REVOKED"}:
            raise CredentialTransitionReceiptError(
                "credential transition receipt operation is invalid"
            )
        if type(self.prior_generation) is not int or self.prior_generation < 1:
            raise CredentialTransitionReceiptError(
                "credential transition prior generation is invalid"
            )
        if self.operation == "ROTATED":
            if (
                type(self.successor_generation) is not int
                or self.successor_generation != self.prior_generation + 1
                or self.active_after is not True
            ):
                raise CredentialTransitionReceiptError(
                    "credential rotation receipt generation state is invalid"
                )
        else:
            if self.successor_generation is not None or self.active_after is not False:
                raise CredentialTransitionReceiptError(
                    "credential revocation receipt terminal state is invalid"
                )
        if type(self.transition_sequence) is not int or self.transition_sequence < 1:
            raise CredentialTransitionReceiptError(
                "credential transition sequence is invalid"
            )
        if self.transition_sequence == 1:
            if self.previous_receipt_id is not None:
                raise CredentialTransitionReceiptError(
                    "first credential transition cannot name a predecessor receipt"
                )
        else:
            if (
                type(self.previous_receipt_id) is not str
                or not self.previous_receipt_id.startswith(
                    "credential-transition/sha256:"
                )
                or len(self.previous_receipt_id)
                != len("credential-transition/sha256:") + 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self.previous_receipt_id[
                        len("credential-transition/sha256:"):
                    ]
                )
            ):
                raise CredentialTransitionReceiptError(
                    "credential transition predecessor receipt id is invalid"
                )
        if type(self.completed_time_ns) is not int or self.completed_time_ns < 1:
            raise CredentialTransitionReceiptError(
                "credential transition completion time is invalid"
            )
        for name in (
            "owner_identity_sha256",
            "vault_authority_sha256",
            "record_state_sha256",
        ):
            value = getattr(self, name)
            if (
                type(value) is not str
                or not value.startswith("sha256:")
                or len(value) != 71
                or any(character not in "0123456789abcdef" for character in value[7:])
            ):
                raise CredentialTransitionReceiptError(
                    f"credential transition receipt {name} is invalid"
                )
        if (
            type(self.receipt_id) is not str
            or not self.receipt_id.startswith("credential-transition/sha256:")
            or len(self.receipt_id) != len("credential-transition/sha256:") + 64
            or any(
                character not in "0123456789abcdef"
                for character in self.receipt_id[len("credential-transition/sha256:"):]
            )
        ):
            raise CredentialTransitionReceiptError(
                "credential transition receipt id is invalid"
            )


_CANONICAL_RECEIPT_TYPE = CredentialTransitionReceipt


def _receipt_subject(
    receipt: CredentialTransitionReceipt,
    *,
    _asdict=_CANONICAL_ASDICT,
) -> dict[str, object]:
    value = _asdict(receipt)
    value.pop("receipt_id")
    return value


def _receipt_id_from_subject(
    subject: dict[str, object],
    *,
    _digest_fn=_digest,
    _canonical_bytes_fn=_canonical_bytes,
) -> str:
    return "credential-transition/" + _digest_fn(_canonical_bytes_fn(subject))


def _parse_receipt(
    value: object,
    *,
    _receipt_id_from_subject_fn=_receipt_id_from_subject,
    _receipt_type=_CANONICAL_RECEIPT_TYPE,
) -> CredentialTransitionReceipt:
    if type(value) is not dict or set(value) != _RECEIPT_FIELDS:
        raise CredentialTransitionReceiptError(
            "stored credential transition receipt shape is invalid"
        )

    # Authenticate the retained bytes before interpreting their semantic fields.
    # Otherwise a tamper that also makes a field combination semantically invalid
    # (for example sequence > 1 with no predecessor) is misclassified before the
    # content-derived receipt id is checked and can obscure the integrity failure.
    claimed_id = value.get("receipt_id")
    subject = dict(value)
    subject.pop("receipt_id")
    try:
        expected_id = _receipt_id_from_subject_fn(subject)
    except (TypeError, ValueError, OverflowError, UnicodeError) as error:
        raise CredentialTransitionReceiptError(
            "stored credential transition receipt content identity is invalid"
        ) from error
    if claimed_id != expected_id:
        raise CredentialTransitionReceiptError(
            "stored credential transition receipt content identity is invalid"
        )

    try:
        return _receipt_type(**value)
    except (TypeError, ValueError, CredentialTransitionReceiptError) as error:
        raise CredentialTransitionReceiptError(
            "stored credential transition receipt is invalid"
        ) from error


def _authority_section(
    state: dict[str, object],
    *,
    create: bool,
    _parse_receipt_fn=_parse_receipt,
    _uuid4=_CANONICAL_UUID4,
    _b64decode=_CANONICAL_B64DECODE,
) -> dict[str, object]:
    section = state.get(_AUTHORITY_KEY)
    if section is None:
        if not create:
            raise CredentialTransitionReceiptError(
                "credential transition authority is not initialized"
            )
        section = {
            "instance_id": _uuid4().hex,
            "latest_by_handle": {},
        }
        state[_AUTHORITY_KEY] = section
    if type(section) is not dict or set(section) != {"instance_id", "latest_by_handle"}:
        raise CredentialTransitionReceiptError(
            "credential transition authority metadata is invalid"
        )
    instance_id = section["instance_id"]
    if (
        type(instance_id) is not str
        or len(instance_id) != 32
        or any(character not in "0123456789abcdef" for character in instance_id)
    ):
        raise CredentialTransitionReceiptError(
            "credential transition authority instance is invalid"
        )
    latest = section["latest_by_handle"]
    if type(latest) is not dict:
        raise CredentialTransitionReceiptError(
            "credential transition receipt index is invalid"
        )
    for handle_id, item in latest.items():
        if type(handle_id) is not str or not handle_id:
            raise CredentialTransitionReceiptError(
                "credential transition receipt handle index is invalid"
            )
        if type(item) is not dict or set(item) != {"receipt", "seal_b64"}:
            raise CredentialTransitionReceiptError(
                "credential transition receipt storage is invalid"
            )
        parsed = _parse_receipt_fn(item["receipt"])
        if parsed.handle_id != handle_id:
            raise CredentialTransitionReceiptError(
                "credential transition receipt handle index mismatches receipt"
            )
        try:
            seal = _b64decode(item["seal_b64"], validate=True)
            if not seal:
                raise ValueError("empty receipt seal")
        except Exception as error:
            raise CredentialTransitionReceiptError(
                "credential transition receipt seal is invalid"
            ) from error
    return section


def _vault_authority_digest(
    vault: ProtectedCredentialVault,
    *,
    instance_id: str,
    _digest_fn=_digest,
    _canonical_bytes_fn=_canonical_bytes,
) -> str:
    return _digest_fn(
        _canonical_bytes_fn(
            {
                "authority": "AUTOTRADE_PROTECTED_CREDENTIAL_VAULT",
                "instance_id": instance_id,
                "vault_path": str(vault.path),
            }
        )
    )


def _record_state_digest(
    record: dict[str, object],
    *,
    _vault_handle=_CANONICAL_VAULT_HANDLE,
    _b64decode=_CANONICAL_B64DECODE,
    _asdict=_CANONICAL_ASDICT,
    _text_digest_fn=_text_digest,
    _digest_fn=_digest,
    _canonical_bytes_fn=_canonical_bytes,
) -> str:
    handle = _vault_handle(record)
    try:
        ciphertext = _b64decode(record["ciphertext"], validate=True)
    except Exception as error:
        raise CredentialTransitionReceiptError(
            "credential transition record ciphertext is invalid"
        ) from error
    if not ciphertext:
        raise CredentialTransitionReceiptError(
            "credential transition record ciphertext is empty"
        )
    owner_identity = record.get("owner_identity")
    if type(owner_identity) is not str or not owner_identity:
        raise CredentialTransitionReceiptError(
            "credential transition record owner identity is invalid"
        )
    active = record.get("active")
    if type(active) is not bool:
        raise CredentialTransitionReceiptError(
            "credential transition record active state is invalid"
        )
    return _digest_fn(
        _canonical_bytes_fn(
            {
                "handle": _asdict(handle),
                "owner_identity_sha256": _text_digest_fn(owner_identity),
                "ciphertext_sha256": _digest_fn(ciphertext),
                "active": active,
            }
        )
    )


def _seal_entropy(
    receipt: CredentialTransitionReceipt,
    *,
    _sha256=_CANONICAL_SHA256,
    _canonical_bytes_fn=_canonical_bytes,
) -> bytes:
    return _sha256(
        _canonical_bytes_fn(
            {
                "purpose": "AUTOTRADE_CREDENTIAL_TRANSITION_RECEIPT_SEAL",
                "receipt_id": receipt.receipt_id,
                "vault_authority_sha256": receipt.vault_authority_sha256,
            }
        )
    ).digest()


def _next_sequence(
    vault: ProtectedCredentialVault,
    section: dict[str, object],
    *,
    handle: PersistentCredentialHandle,
    _parse_receipt_fn=_parse_receipt,
    _receipt_id_from_subject_fn=_receipt_id_from_subject,
    _receipt_subject_fn=_receipt_subject,
    _vault_authority_digest_fn=_vault_authority_digest,
    _seal_entropy_fn=_seal_entropy,
    _b64decode=_CANONICAL_B64DECODE,
) -> tuple[int, str | None]:
    previous = section["latest_by_handle"].get(handle.handle_id)
    if previous is None:
        return 1, None

    parsed = _parse_receipt_fn(previous["receipt"])
    expected_id = _receipt_id_from_subject_fn(_receipt_subject_fn(parsed))
    if parsed.receipt_id != expected_id:
        raise CredentialTransitionReceiptError(
            "prior credential transition receipt content identity is invalid"
        )
    expected_authority = _vault_authority_digest_fn(
        vault,
        instance_id=section["instance_id"],
    )
    if parsed.vault_authority_sha256 != expected_authority:
        raise CredentialTransitionReceiptError(
            "prior credential transition receipt vault authority is invalid"
        )
    if (
        parsed.handle_id != handle.handle_id
        or parsed.account_id != handle.account_id
        or parsed.provider != handle.provider
        or parsed.environment != handle.environment
        or parsed.provider_environment != handle.provider_environment
        or parsed.purpose != handle.purpose
    ):
        raise CredentialTransitionReceiptError(
            "prior credential transition receipt scope does not match current credential"
        )
    try:
        sealed = _b64decode(previous["seal_b64"], validate=True)
        unsealed = vault._protector.unprotect(
            sealed,
            entropy=_seal_entropy_fn(parsed),
        )
    except Exception as error:
        raise CredentialTransitionReceiptError(
            "prior credential transition receipt issuer seal is invalid"
        ) from error
    if unsealed != parsed.receipt_id.encode("utf-8"):
        raise CredentialTransitionReceiptError(
            "prior credential transition receipt issuer seal payload mismatch"
        )
    return parsed.transition_sequence + 1, parsed.receipt_id


def _issue_locked(
    vault: ProtectedCredentialVault,
    state: dict[str, object],
    *,
    prior_handle: PersistentCredentialHandle,
    current_handle: PersistentCredentialHandle,
    operation: str,
    _authority_section_fn=_authority_section,
    _vault_authority_digest_fn=_vault_authority_digest,
    _next_sequence_fn=_next_sequence,
    _record_state_digest_fn=_record_state_digest,
    _receipt_id_from_subject_fn=_receipt_id_from_subject,
    _seal_entropy_fn=_seal_entropy,
    _text_digest_fn=_text_digest,
    _asdict=_CANONICAL_ASDICT,
    _b64encode=_CANONICAL_B64ENCODE,
    _receipt_type=_CANONICAL_RECEIPT_TYPE,
    _clock=_CANONICAL_TIME_NS,
) -> CredentialTransitionReceipt:
    if prior_handle.purpose != "TRADE" or current_handle.purpose != "TRADE":
        raise CredentialTransitionReceiptError(
            "READ credentials cannot issue sender-fence transition receipts"
        )
    if prior_handle.provider_environment != current_handle.provider_environment:
        raise CredentialTransitionReceiptError(
            "credential transition cannot change provider_environment"
        )
    record = state["records"].get(current_handle.handle_id)
    if record is None:
        raise CredentialTransitionReceiptError(
            "credential transition record disappeared before receipt issuance"
        )
    section = _authority_section_fn(state, create=True)
    owner_identity = record["owner_identity"]
    authority_digest = _vault_authority_digest_fn(
        vault,
        instance_id=section["instance_id"],
    )
    sequence, previous_receipt_id = _next_sequence_fn(
        vault,
        section,
        handle=current_handle,
    )
    subject = {
        "schema_version": _SCHEMA_VERSION,
        "operation": operation,
        "handle_id": current_handle.handle_id,
        "account_id": current_handle.account_id,
        "provider": current_handle.provider,
        "environment": current_handle.environment,
        "provider_environment": current_handle.provider_environment,
        "purpose": current_handle.purpose,
        "prior_generation": prior_handle.generation,
        "successor_generation": (
            current_handle.generation if operation == "ROTATED" else None
        ),
        "active_after": bool(record["active"]),
        "owner_identity_sha256": _text_digest_fn(owner_identity),
        "vault_authority_sha256": authority_digest,
        "record_state_sha256": _record_state_digest_fn(record),
        "previous_receipt_id": previous_receipt_id,
        "transition_sequence": sequence,
        "completed_time_ns": _clock(),
    }
    receipt = _receipt_type(
        receipt_id=_receipt_id_from_subject_fn(subject),
        **subject,
    )
    try:
        seal = vault._protector.protect(
            receipt.receipt_id.encode("utf-8"),
            entropy=_seal_entropy_fn(receipt),
        )
    except Exception as error:
        raise CredentialTransitionReceiptError(
            "credential transition receipt issuer seal failed"
        ) from error
    if type(seal) is not bytes or not seal:
        raise CredentialTransitionReceiptError(
            "credential transition receipt issuer seal is invalid"
        )
    section["latest_by_handle"][receipt.handle_id] = {
        "receipt": _asdict(receipt),
        "seal_b64": _b64encode(seal).decode("ascii"),
    }
    return receipt


def rotate_trade_credential_with_receipt(
    vault: ProtectedCredentialVault,
    handle: PersistentCredentialHandle,
    *,
    execution_identity: str,
    new_secret_value: str,
    _vault_type=_CANONICAL_VAULT_TYPE,
    _handle_type=_CANONICAL_HANDLE_TYPE,
    _file_lock=_CANONICAL_EXCLUSIVE_FILE_LOCK,
    _vault_load=_CANONICAL_VAULT_LOAD,
    _vault_handle=_CANONICAL_VAULT_HANDLE,
    _prove_identity=_CANONICAL_VAULT_PROVE_CURRENT_IDENTITY,
    _vault_write=_CANONICAL_VAULT_WRITE,
    _scope_entropy_fn=_CANONICAL_SCOPE_ENTROPY,
    _text_fn=_CANONICAL_TEXT,
    _issue_locked_fn=_issue_locked,
    _b64encode=_CANONICAL_B64ENCODE,
) -> tuple[PersistentCredentialHandle, CredentialTransitionReceipt]:
    """Rotate one exact TRADE generation and atomically retain its receipt."""

    if type(vault) is not _vault_type:
        raise TypeError("vault must be an exact ProtectedCredentialVault")
    if type(handle) is not _handle_type:
        raise TypeError("handle must be an exact PersistentCredentialHandle")
    if handle.purpose != "TRADE":
        raise CredentialTransitionReceiptError(
            "READ credentials cannot issue sender-fence transition receipts"
        )
    if type(new_secret_value) is not str or not new_secret_value:
        raise SecretVaultError("new_secret_value must not be empty")
    owner = _text_fn(execution_identity, name="execution_identity")

    with _file_lock(vault.lock_path, vault_path=vault.path):
        state = _vault_load(vault)
        record = state["records"].get(handle.handle_id)
        if record is None or record["active"] is not True:
            raise PermissionError("Credential is unavailable")
        current = _vault_handle(record)
        if current != handle:
            raise PermissionError("Credential handle generation is stale")
        if current.purpose != "TRADE":
            raise CredentialTransitionReceiptError(
                "READ credentials cannot issue sender-fence transition receipts"
            )
        if record["owner_identity"] != owner:
            raise PermissionError("Secret identity mismatch")
        _prove_identity(
            vault,
            record,
            current,
            owner_identity=owner,
        )
        next_handle = _handle_type(
            handle_id=current.handle_id,
            account_id=current.account_id,
            provider=current.provider,
            environment=current.environment,
            provider_environment=current.provider_environment,
            purpose=current.purpose,
            generation=current.generation + 1,
        )
        entropy = _scope_entropy_fn(
            handle_id=next_handle.handle_id,
            owner_identity=owner,
            account_id=next_handle.account_id,
            provider=next_handle.provider,
            environment=next_handle.environment,
            provider_environment=next_handle.provider_environment,
            purpose=next_handle.purpose,
            generation=next_handle.generation,
        )
        record["handle"] = _CANONICAL_ASDICT(next_handle)
        record["ciphertext"] = _b64encode(
            vault._protector.protect(
                new_secret_value.encode("utf-8"),
                entropy=entropy,
            )
        ).decode("ascii")
        receipt = _issue_locked_fn(
            vault,
            state,
            prior_handle=current,
            current_handle=next_handle,
            operation="ROTATED",
        )
        _vault_write(vault, state)
        return next_handle, receipt


def revoke_trade_credential_with_receipt(
    vault: ProtectedCredentialVault,
    handle: PersistentCredentialHandle,
    *,
    execution_identity: str,
    _vault_type=_CANONICAL_VAULT_TYPE,
    _handle_type=_CANONICAL_HANDLE_TYPE,
    _file_lock=_CANONICAL_EXCLUSIVE_FILE_LOCK,
    _vault_load=_CANONICAL_VAULT_LOAD,
    _vault_handle=_CANONICAL_VAULT_HANDLE,
    _prove_identity=_CANONICAL_VAULT_PROVE_CURRENT_IDENTITY,
    _vault_write=_CANONICAL_VAULT_WRITE,
    _text_fn=_CANONICAL_TEXT,
    _issue_locked_fn=_issue_locked,
    _b64encode=_CANONICAL_B64ENCODE,
    _random_bytes=_CANONICAL_URANDOM,
) -> CredentialTransitionReceipt:
    """Revoke one exact TRADE generation and atomically retain its receipt."""

    if type(vault) is not _vault_type:
        raise TypeError("vault must be an exact ProtectedCredentialVault")
    if type(handle) is not _handle_type:
        raise TypeError("handle must be an exact PersistentCredentialHandle")
    if handle.purpose != "TRADE":
        raise CredentialTransitionReceiptError(
            "READ credentials cannot issue sender-fence transition receipts"
        )
    owner = _text_fn(execution_identity, name="execution_identity")

    with _file_lock(vault.lock_path, vault_path=vault.path):
        state = _vault_load(vault)
        record = state["records"].get(handle.handle_id)
        if record is None or record["active"] is not True:
            raise PermissionError("Credential is unavailable")
        current = _vault_handle(record)
        if current != handle:
            raise PermissionError("Credential handle generation is stale")
        if current.purpose != "TRADE":
            raise CredentialTransitionReceiptError(
                "READ credentials cannot issue sender-fence transition receipts"
            )
        if record["owner_identity"] != owner:
            raise PermissionError("Secret identity mismatch")
        _prove_identity(
            vault,
            record,
            current,
            owner_identity=owner,
        )
        record["active"] = False
        record["ciphertext"] = _b64encode(_random_bytes(32)).decode("ascii")
        receipt = _issue_locked_fn(
            vault,
            state,
            prior_handle=current,
            current_handle=current,
            operation="REVOKED",
        )
        _vault_write(vault, state)
        return receipt


def verify_trade_credential_transition_receipt(
    vault: ProtectedCredentialVault,
    receipt: CredentialTransitionReceipt,
    *,
    _vault_type=_CANONICAL_VAULT_TYPE,
    _receipt_type=_CANONICAL_RECEIPT_TYPE,
    _file_lock=_CANONICAL_EXCLUSIVE_FILE_LOCK,
    _vault_load=_CANONICAL_VAULT_LOAD,
    _vault_handle=_CANONICAL_VAULT_HANDLE,
    _authority_section_fn=_authority_section,
    _parse_receipt_fn=_parse_receipt,
    _receipt_id_from_subject_fn=_receipt_id_from_subject,
    _receipt_subject_fn=_receipt_subject,
    _vault_authority_digest_fn=_vault_authority_digest,
    _text_digest_fn=_text_digest,
    _record_state_digest_fn=_record_state_digest,
    _seal_entropy_fn=_seal_entropy,
    _b64decode=_CANONICAL_B64DECODE,
) -> CredentialTransitionReceipt:
    """Revalidate one receipt against the exact current canonical vault state."""

    if type(vault) is not _vault_type:
        raise TypeError("vault must be an exact ProtectedCredentialVault")
    if type(receipt) is not _receipt_type:
        raise TypeError("receipt must be an exact CredentialTransitionReceipt")

    with _file_lock(vault.lock_path, vault_path=vault.path):
        state = _vault_load(vault)
        section = _authority_section_fn(state, create=False)
        stored = section["latest_by_handle"].get(receipt.handle_id)
        if stored is None:
            raise CredentialTransitionReceiptError(
                "credential transition receipt is not vault-issued current evidence"
            )
        stored_receipt = _parse_receipt_fn(stored["receipt"])
        if stored_receipt != receipt:
            raise CredentialTransitionReceiptError(
                "credential transition receipt is stale or not vault-issued"
            )
        expected_id = _receipt_id_from_subject_fn(_receipt_subject_fn(receipt))
        if receipt.receipt_id != expected_id:
            raise CredentialTransitionReceiptError(
                "credential transition receipt content identity mismatch"
            )
        expected_authority = _vault_authority_digest_fn(
            vault,
            instance_id=section["instance_id"],
        )
        if receipt.vault_authority_sha256 != expected_authority:
            raise CredentialTransitionReceiptError(
                "credential transition receipt belongs to a different vault authority"
            )

        record = state["records"].get(receipt.handle_id)
        if record is None:
            raise CredentialTransitionReceiptError(
                "credential transition current record is missing"
            )
        current = _vault_handle(record)
        if (
            current.handle_id != receipt.handle_id
            or current.account_id != receipt.account_id
            or current.provider != receipt.provider
            or current.environment != receipt.environment
            or current.provider_environment != receipt.provider_environment
            or current.purpose != receipt.purpose
        ):
            raise CredentialTransitionReceiptError(
                "credential transition current scope mismatches receipt"
            )
        if receipt.operation == "ROTATED":
            if (
                current.generation != receipt.successor_generation
                or record["active"] is not True
            ):
                raise CredentialTransitionReceiptError(
                    "credential rotation receipt is no longer current"
                )
        else:
            if (
                current.generation != receipt.prior_generation
                or record["active"] is not False
            ):
                raise CredentialTransitionReceiptError(
                    "credential revocation receipt is no longer current"
                )
        owner_identity = record["owner_identity"]
        if _text_digest_fn(owner_identity) != receipt.owner_identity_sha256:
            raise CredentialTransitionReceiptError(
                "credential transition owner identity digest mismatch"
            )
        if _record_state_digest_fn(record) != receipt.record_state_sha256:
            raise CredentialTransitionReceiptError(
                "credential transition current record state changed"
            )

        try:
            sealed = _b64decode(stored["seal_b64"], validate=True)
            unsealed = vault._protector.unprotect(
                sealed,
                entropy=_seal_entropy_fn(receipt),
            )
        except Exception as error:
            raise CredentialTransitionReceiptError(
                "credential transition receipt issuer seal cannot be verified"
            ) from error
        if unsealed != receipt.receipt_id.encode("utf-8"):
            raise CredentialTransitionReceiptError(
                "credential transition receipt issuer seal payload mismatch"
            )
        return receipt


def _bind_rotate_entrypoint(implementation):
    def rotate_trade_credential_with_receipt(
        vault: ProtectedCredentialVault,
        handle: PersistentCredentialHandle,
        *,
        execution_identity: str,
        new_secret_value: str,
    ) -> tuple[PersistentCredentialHandle, CredentialTransitionReceipt]:
        return implementation(
            vault,
            handle,
            execution_identity=execution_identity,
            new_secret_value=new_secret_value,
        )

    rotate_trade_credential_with_receipt.__doc__ = implementation.__doc__
    return rotate_trade_credential_with_receipt


def _bind_revoke_entrypoint(implementation):
    def revoke_trade_credential_with_receipt(
        vault: ProtectedCredentialVault,
        handle: PersistentCredentialHandle,
        *,
        execution_identity: str,
    ) -> CredentialTransitionReceipt:
        return implementation(
            vault,
            handle,
            execution_identity=execution_identity,
        )

    revoke_trade_credential_with_receipt.__doc__ = implementation.__doc__
    return revoke_trade_credential_with_receipt


def _bind_verify_entrypoint(implementation):
    def verify_trade_credential_transition_receipt(
        vault: ProtectedCredentialVault,
        receipt: CredentialTransitionReceipt,
    ) -> CredentialTransitionReceipt:
        return implementation(vault, receipt)

    verify_trade_credential_transition_receipt.__doc__ = implementation.__doc__
    return verify_trade_credential_transition_receipt


_rotate_trade_credential_with_receipt_impl = rotate_trade_credential_with_receipt
_revoke_trade_credential_with_receipt_impl = revoke_trade_credential_with_receipt
_verify_trade_credential_transition_receipt_impl = (
    verify_trade_credential_transition_receipt
)

rotate_trade_credential_with_receipt = _bind_rotate_entrypoint(
    _rotate_trade_credential_with_receipt_impl
)
revoke_trade_credential_with_receipt = _bind_revoke_entrypoint(
    _revoke_trade_credential_with_receipt_impl
)
verify_trade_credential_transition_receipt = _bind_verify_entrypoint(
    _verify_trade_credential_transition_receipt_impl
)
