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


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _text_digest(value: str) -> str:
    return _digest(value.encode("utf-8"))


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

    def __post_init__(self) -> None:
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
            normalized_provider_environment = normalize_provider_environment(
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


def _receipt_subject(receipt: CredentialTransitionReceipt) -> dict[str, object]:
    value = asdict(receipt)
    value.pop("receipt_id")
    return value


def _receipt_id_from_subject(subject: dict[str, object]) -> str:
    return "credential-transition/" + _digest(_canonical_bytes(subject))


def _parse_receipt(value: object) -> CredentialTransitionReceipt:
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
        expected_id = _receipt_id_from_subject(subject)
    except (TypeError, ValueError, OverflowError) as error:
        raise CredentialTransitionReceiptError(
            "stored credential transition receipt content identity is invalid"
        ) from error
    if claimed_id != expected_id:
        raise CredentialTransitionReceiptError(
            "stored credential transition receipt content identity is invalid"
        )

    try:
        return CredentialTransitionReceipt(**value)
    except (TypeError, ValueError, CredentialTransitionReceiptError) as error:
        raise CredentialTransitionReceiptError(
            "stored credential transition receipt is invalid"
        ) from error


def _authority_section(
    state: dict[str, object],
    *,
    create: bool,
) -> dict[str, object]:
    section = state.get(_AUTHORITY_KEY)
    if section is None:
        if not create:
            raise CredentialTransitionReceiptError(
                "credential transition authority is not initialized"
            )
        section = {
            "instance_id": uuid4().hex,
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
        parsed = _parse_receipt(item["receipt"])
        if parsed.handle_id != handle_id:
            raise CredentialTransitionReceiptError(
                "credential transition receipt handle index mismatches receipt"
            )
        try:
            seal = b64decode(item["seal_b64"], validate=True)
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
) -> str:
    return _digest(
        _canonical_bytes(
            {
                "authority": "AUTOTRADE_PROTECTED_CREDENTIAL_VAULT",
                "instance_id": instance_id,
                "vault_path": str(vault.path),
            }
        )
    )


def _record_state_digest(record: dict[str, object]) -> str:
    handle = ProtectedCredentialVault._handle(record)
    try:
        ciphertext = b64decode(record["ciphertext"], validate=True)
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
    return _digest(
        _canonical_bytes(
            {
                "handle": asdict(handle),
                "owner_identity_sha256": _text_digest(owner_identity),
                "ciphertext_sha256": _digest(ciphertext),
                "active": active,
            }
        )
    )


def _seal_entropy(receipt: CredentialTransitionReceipt) -> bytes:
    return sha256(
        _canonical_bytes(
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
) -> tuple[int, str | None]:
    previous = section["latest_by_handle"].get(handle.handle_id)
    if previous is None:
        return 1, None

    parsed = _parse_receipt(previous["receipt"])
    expected_id = _receipt_id_from_subject(_receipt_subject(parsed))
    if parsed.receipt_id != expected_id:
        raise CredentialTransitionReceiptError(
            "prior credential transition receipt content identity is invalid"
        )
    expected_authority = _vault_authority_digest(
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
        sealed = b64decode(previous["seal_b64"], validate=True)
        unsealed = vault._protector.unprotect(
            sealed,
            entropy=_seal_entropy(parsed),
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
    section = _authority_section(state, create=True)
    owner_identity = record["owner_identity"]
    authority_digest = _vault_authority_digest(
        vault,
        instance_id=section["instance_id"],
    )
    sequence, previous_receipt_id = _next_sequence(
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
        "owner_identity_sha256": _text_digest(owner_identity),
        "vault_authority_sha256": authority_digest,
        "record_state_sha256": _record_state_digest(record),
        "previous_receipt_id": previous_receipt_id,
        "transition_sequence": sequence,
        "completed_time_ns": time_ns(),
    }
    receipt = CredentialTransitionReceipt(
        receipt_id=_receipt_id_from_subject(subject),
        **subject,
    )
    try:
        seal = vault._protector.protect(
            receipt.receipt_id.encode("utf-8"),
            entropy=_seal_entropy(receipt),
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
        "receipt": asdict(receipt),
        "seal_b64": b64encode(seal).decode("ascii"),
    }
    return receipt


def rotate_trade_credential_with_receipt(
    vault: ProtectedCredentialVault,
    handle: PersistentCredentialHandle,
    *,
    execution_identity: str,
    new_secret_value: str,
) -> tuple[PersistentCredentialHandle, CredentialTransitionReceipt]:
    """Rotate one exact TRADE generation and atomically retain its receipt."""

    if type(vault) is not ProtectedCredentialVault:
        raise TypeError("vault must be an exact ProtectedCredentialVault")
    if type(handle) is not PersistentCredentialHandle:
        raise TypeError("handle must be an exact PersistentCredentialHandle")
    if handle.purpose != "TRADE":
        raise CredentialTransitionReceiptError(
            "READ credentials cannot issue sender-fence transition receipts"
        )
    if type(new_secret_value) is not str or not new_secret_value:
        raise SecretVaultError("new_secret_value must not be empty")
    owner = _text(execution_identity, name="execution_identity")

    with _exclusive_file_lock(vault.lock_path, vault_path=vault.path):
        state = ProtectedCredentialVault._load(vault)
        record = state["records"].get(handle.handle_id)
        if record is None or record["active"] is not True:
            raise PermissionError("Credential is unavailable")
        current = ProtectedCredentialVault._handle(record)
        if current != handle:
            raise PermissionError("Credential handle generation is stale")
        if current.purpose != "TRADE":
            raise CredentialTransitionReceiptError(
                "READ credentials cannot issue sender-fence transition receipts"
            )
        if record["owner_identity"] != owner:
            raise PermissionError("Secret identity mismatch")
        ProtectedCredentialVault._prove_current_identity_can_decrypt(
            vault,
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
            vault._protector.protect(
                new_secret_value.encode("utf-8"),
                entropy=entropy,
            )
        ).decode("ascii")
        receipt = _issue_locked(
            vault,
            state,
            prior_handle=current,
            current_handle=next_handle,
            operation="ROTATED",
        )
        ProtectedCredentialVault._write(vault, state)
        return next_handle, receipt


def revoke_trade_credential_with_receipt(
    vault: ProtectedCredentialVault,
    handle: PersistentCredentialHandle,
    *,
    execution_identity: str,
) -> CredentialTransitionReceipt:
    """Revoke one exact TRADE generation and atomically retain its receipt."""

    if type(vault) is not ProtectedCredentialVault:
        raise TypeError("vault must be an exact ProtectedCredentialVault")
    if type(handle) is not PersistentCredentialHandle:
        raise TypeError("handle must be an exact PersistentCredentialHandle")
    if handle.purpose != "TRADE":
        raise CredentialTransitionReceiptError(
            "READ credentials cannot issue sender-fence transition receipts"
        )
    owner = _text(execution_identity, name="execution_identity")

    with _exclusive_file_lock(vault.lock_path, vault_path=vault.path):
        state = ProtectedCredentialVault._load(vault)
        record = state["records"].get(handle.handle_id)
        if record is None or record["active"] is not True:
            raise PermissionError("Credential is unavailable")
        current = ProtectedCredentialVault._handle(record)
        if current != handle:
            raise PermissionError("Credential handle generation is stale")
        if current.purpose != "TRADE":
            raise CredentialTransitionReceiptError(
                "READ credentials cannot issue sender-fence transition receipts"
            )
        if record["owner_identity"] != owner:
            raise PermissionError("Secret identity mismatch")
        ProtectedCredentialVault._prove_current_identity_can_decrypt(
            vault,
            record,
            current,
            owner_identity=owner,
        )
        record["active"] = False
        record["ciphertext"] = b64encode(os.urandom(32)).decode("ascii")
        receipt = _issue_locked(
            vault,
            state,
            prior_handle=current,
            current_handle=current,
            operation="REVOKED",
        )
        ProtectedCredentialVault._write(vault, state)
        return receipt


def verify_trade_credential_transition_receipt(
    vault: ProtectedCredentialVault,
    receipt: CredentialTransitionReceipt,
) -> CredentialTransitionReceipt:
    """Revalidate one receipt against the exact current canonical vault state."""

    if type(vault) is not ProtectedCredentialVault:
        raise TypeError("vault must be an exact ProtectedCredentialVault")
    if type(receipt) is not CredentialTransitionReceipt:
        raise TypeError("receipt must be an exact CredentialTransitionReceipt")

    with _exclusive_file_lock(vault.lock_path, vault_path=vault.path):
        state = ProtectedCredentialVault._load(vault)
        section = _authority_section(state, create=False)
        stored = section["latest_by_handle"].get(receipt.handle_id)
        if stored is None:
            raise CredentialTransitionReceiptError(
                "credential transition receipt is not vault-issued current evidence"
            )
        stored_receipt = _parse_receipt(stored["receipt"])
        if stored_receipt != receipt:
            raise CredentialTransitionReceiptError(
                "credential transition receipt is stale or not vault-issued"
            )
        expected_id = _receipt_id_from_subject(_receipt_subject(receipt))
        if receipt.receipt_id != expected_id:
            raise CredentialTransitionReceiptError(
                "credential transition receipt content identity mismatch"
            )
        expected_authority = _vault_authority_digest(
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
        current = ProtectedCredentialVault._handle(record)
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
        if _text_digest(owner_identity) != receipt.owner_identity_sha256:
            raise CredentialTransitionReceiptError(
                "credential transition owner identity digest mismatch"
            )
        if _record_state_digest(record) != receipt.record_state_sha256:
            raise CredentialTransitionReceiptError(
                "credential transition current record state changed"
            )

        try:
            sealed = b64decode(stored["seal_b64"], validate=True)
            unsealed = vault._protector.unprotect(
                sealed,
                entropy=_seal_entropy(receipt),
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
