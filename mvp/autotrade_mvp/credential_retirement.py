"""Live credential-generation retirement observation for recovery takeover composition.

This module deliberately does not issue durable takeover authority.  It is a
read-only adapter over the one canonical ProtectedCredentialVault state and is
intended for the future WP-49 takeover issuer to re-check immediately before an
owner transition, alongside an independently held host/process lease.

A previously captured snapshot is never sufficient on its own: callers must
re-run ``require_retired_trade_credential_generation`` at the authoritative
cut.  That property is important because the credential vault is mutable and
its file cannot be treated as an anti-rollback journal.
"""

from __future__ import annotations

from dataclasses import dataclass

from .windows_secrets import (
    PersistentCredentialHandle,
    ProtectedCredentialVault,
    _exclusive_file_lock,
)


class CredentialRetirementError(PermissionError):
    """Raised when an old TRADE credential generation is not provably retired."""


@dataclass(frozen=True)
class CredentialGenerationRetirementSnapshot:
    """Detached non-secret observation of one retired TRADE generation.

    This value is diagnostic/compositional evidence only.  It is intentionally
    not self-authenticating and must not be accepted later without re-reading
    the selected canonical vault at the takeover cut.
    """

    retired_handle: PersistentCredentialHandle
    disposition: str
    observed_generation: int
    observed_active: bool

    def __post_init__(self) -> None:
        if type(self.retired_handle) is not PersistentCredentialHandle:
            raise TypeError("retired_handle must be an exact PersistentCredentialHandle")
        if self.retired_handle.purpose != "TRADE":
            raise ValueError("credential retirement snapshot must bind TRADE purpose")
        if self.disposition not in {"REVOKED", "SUPERSEDED"}:
            raise ValueError("credential retirement disposition is invalid")
        if (
            type(self.observed_generation) is not int
            or self.observed_generation < self.retired_handle.generation
        ):
            raise ValueError("observed credential generation is invalid")
        if type(self.observed_active) is not bool:
            raise ValueError("observed_active must be boolean")
        if self.disposition == "REVOKED":
            if self.observed_generation != self.retired_handle.generation:
                raise ValueError("revoked snapshot generation mismatch")
            if self.observed_active:
                raise ValueError("revoked snapshot cannot report an active generation")
        else:
            if self.observed_generation <= self.retired_handle.generation:
                raise ValueError("superseded snapshot must observe a newer generation")


def _same_scope(
    left: PersistentCredentialHandle,
    right: PersistentCredentialHandle,
) -> bool:
    return (
        left.handle_id == right.handle_id
        and left.account_id == right.account_id
        and left.provider == right.provider
        and left.environment == right.environment
        and left.purpose == right.purpose
    )


def require_retired_trade_credential_generation(
    vault: ProtectedCredentialVault,
    handle: PersistentCredentialHandle,
) -> CredentialGenerationRetirementSnapshot:
    """Re-check that one exact TRADE handle can no longer acquire a vault lease.

    The check executes under the vault's canonical inter-process generation
    lock and uses class-qualified vault parsing so a caller-supplied subclass
    cannot turn an overridden reader into retirement authority.

    Retirement is accepted only when either:
    * the exact generation is currently inactive (``REVOKED``), or
    * the canonical record has advanced to a strictly newer generation
      (``SUPERSEDED``).

    Missing records, active exact generations, scope drift, and apparent
    generation rollback all fail closed.  The returned snapshot does not grant
    recovery ownership by itself; WP-49 must compose this live re-check with
    the independent host/process lease fence and journal owner transition.
    """

    if type(vault) is not ProtectedCredentialVault:
        raise TypeError("vault must be an exact ProtectedCredentialVault")
    if type(handle) is not PersistentCredentialHandle:
        raise TypeError("handle must be an exact PersistentCredentialHandle")
    if handle.purpose != "TRADE":
        raise CredentialRetirementError(
            "Only a TRADE credential generation can satisfy sender retirement"
        )

    vault_path = vault.path
    lock_path = vault.lock_path
    with _exclusive_file_lock(lock_path, vault_path=vault_path):
        state = ProtectedCredentialVault._load(vault)
        records = state["records"]
        record = records.get(handle.handle_id)
        if record is None:
            raise CredentialRetirementError(
                "Credential generation retirement cannot be proven from a missing record"
            )

        current = ProtectedCredentialVault._handle(record)
        if not _same_scope(current, handle):
            raise CredentialRetirementError(
                "Credential generation retirement scope does not match the selected handle"
            )
        if current.generation < handle.generation:
            raise CredentialRetirementError(
                "Credential vault generation is older than the selected retired handle"
            )

        active = record["active"]
        if type(active) is not bool:
            raise CredentialRetirementError(
                "Credential generation active state is invalid"
            )

        if current.generation == handle.generation:
            if active:
                raise CredentialRetirementError(
                    "Credential generation is still active"
                )
            disposition = "REVOKED"
        else:
            disposition = "SUPERSEDED"

        return CredentialGenerationRetirementSnapshot(
            retired_handle=handle,
            disposition=disposition,
            observed_generation=current.generation,
            observed_active=active,
        )
