"""One canonical content identity for the provider-origin JournalStore.

Host durability writers and Host/provider-origin verification must bind the same
physical JournalStore generation. This module owns that identity so the two
composition paths cannot silently drift to different hashing schemes.
"""

from __future__ import annotations

from .persistence import (
    JournalStore,
    payload_digest,
    require_exact_journal_store_authority,
)
from .store_identity import (
    JournalStoreIdentity,
    require_exact_journal_store_identity,
)


class ProviderOriginJournalIdentityError(RuntimeError):
    """The canonical provider-origin journal identity cannot be established."""


_JOURNAL_IDENTITY_SCHEMA = "autotrade-provider-origin-journal-identity:v1"


def _journal_identity_material(
    identity: JournalStoreIdentity,
    _require_identity=require_exact_journal_store_identity,
) -> dict[str, object]:
    exact = _require_identity(
        identity,
        subject="provider-origin journal identity",
    )
    state = vars(exact)
    source = state["identity_source"]
    if source == "windows_by_handle":
        # Windows path spelling is not backing-object authority. The retained
        # HANDLE identity is the same authority used by same_journal_backing_object.
        return {
            "schema": _JOURNAL_IDENTITY_SCHEMA,
            "identity_source": source,
            "windows_volume_serial": state["windows_volume_serial"],
            "windows_file_index_high": state["windows_file_index_high"],
            "windows_file_index_low": state["windows_file_index_low"],
        }
    if source == "posix_stat":
        return {
            "schema": _JOURNAL_IDENTITY_SCHEMA,
            "identity_source": source,
            "canonical_path": state["canonical_path"],
            "filesystem_device": state["filesystem_device"],
            "filesystem_inode": state["filesystem_inode"],
        }
    raise ProviderOriginJournalIdentityError(
        "provider-origin journal identity source is unsupported"
    )


def canonical_provider_origin_journal_identity(
    store: JournalStore,
    _require_store=require_exact_journal_store_authority,
    _material=_journal_identity_material,
    _digest=payload_digest,
) -> str:
    """Content-address the exact selected physical JournalStore generation."""

    try:
        identity = _require_store(
            store,
            subject="provider-origin canonical JournalStore",
        )
        digest = _digest(_material(identity))
    except ProviderOriginJournalIdentityError:
        raise
    except (TypeError, RuntimeError, ValueError) as error:
        raise ProviderOriginJournalIdentityError(
            "canonical provider-origin JournalStore authority is unavailable"
        ) from error
    if (
        type(digest) is not str
        or not digest.startswith("sha256:")
        or len(digest) != 71
    ):
        raise ProviderOriginJournalIdentityError(
            "canonical provider-origin journal identity is invalid"
        )
    return digest
