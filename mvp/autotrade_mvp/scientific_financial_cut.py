"""Bind scientific qualification to one exact financial journal cut.

The primitive in this module is provenance only. It does not calculate or bless
profitability, economic edge, financial invariants, or research metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from .persistence import (
    JournalStore,
    canonical_json,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .provider_domain import normalize_provider_environment
from .reconciliation_journal import (
    load_latest_reconciliation_checkpoint_for_scope,
    require_current_reconciliation_checkpoint,
)


class FinancialCutUnavailable(LookupError):
    """The required current financial evidence does not exist yet."""


class FinancialCutConflict(RuntimeError):
    """The selected financial authority is contradictory, stale, or raced."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact built-in text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _sha256(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if (
        len(text) != 71
        or not text.startswith("sha256:")
        or any(ch not in "0123456789abcdef" for ch in text[7:])
    ):
        raise ValueError(f"{name} must be a canonical SHA-256 digest")
    return text


def _exact_dict(value: object, *, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise FinancialCutConflict(f"{name} must be an exact dictionary")
    if any(type(key) is not str for key in value):
        raise FinancialCutConflict(f"{name} keys must be exact text")
    return dict(value)


def _journal_sequence(state: dict[str, Any]) -> int:
    value = state.get("journal_sequence")
    if type(value) is not int or value < 0:
        raise FinancialCutConflict(
            "whole-store financial cut lacks a canonical journal_sequence"
        )
    return value


def _digest_exact_journal_population(
    store: JournalStore,
    *,
    target_sequence: int,
    page_size: int = 1000,
) -> str:
    """Hash the exact append-only population through one frozen sequence.

    The encoding is byte-for-byte the same canonical JSON representation used
    by payload_digest(list_of_events), but pages are released as they are read
    so qualification memory remains bounded by page_size rather than journal
    lifetime.
    """

    if type(target_sequence) is not int or target_sequence < 0:
        raise FinancialCutConflict("target journal sequence is invalid")
    if type(page_size) is not int or page_size <= 0 or page_size > 100000:
        raise ValueError("page_size must be between 1 and 100000")

    digest = sha256()
    digest.update(b"[")
    after_sequence = 0
    first_event = True
    while after_sequence < target_sequence:
        remaining = target_sequence - after_sequence
        try:
            batch = JournalStore.load_events_after_journal_sequence(
                store,
                after_sequence,
                limit=min(page_size, remaining),
            )
        except ValueError as error:
            raise FinancialCutConflict(
                "journal population is not contiguous at the frozen cut"
            ) from error
        if type(batch) is not list or not batch:
            raise FinancialCutConflict(
                "journal population ended before the frozen journal sequence"
            )
        for raw in batch:
            event = _exact_dict(raw, name="journal_event")
            sequence = event.get("journal_sequence")
            expected = after_sequence + 1
            if type(sequence) is not int or sequence != expected:
                raise FinancialCutConflict(
                    "journal population is not contiguous at the frozen cut"
                )
            if sequence > target_sequence:
                raise FinancialCutConflict(
                    "journal reader crossed the frozen financial cut"
                )
            if not first_event:
                digest.update(b",")
            digest.update(canonical_json(event).encode("utf-8"))
            first_event = False
            after_sequence = sequence
    if after_sequence != target_sequence:
        raise FinancialCutConflict(
            "journal population cardinality does not match the frozen sequence"
        )
    digest.update(b"]")
    return "sha256:" + digest.hexdigest()


def _event_at_journal_sequence(
    store: JournalStore,
    *,
    journal_sequence: int,
) -> dict[str, Any]:
    if type(journal_sequence) is not int or journal_sequence <= 0:
        raise FinancialCutConflict("journal event sequence must be positive")
    try:
        events = JournalStore.load_events_after_journal_sequence(
            store,
            journal_sequence - 1,
            limit=1,
        )
    except ValueError as error:
        raise FinancialCutConflict(
            "journal event slot is not contiguous"
        ) from error
    if type(events) is not list or len(events) != 1:
        raise FinancialCutConflict("journal event slot is unavailable")
    event = _exact_dict(events[0], name="journal_event_slot")
    if event.get("journal_sequence") != journal_sequence:
        raise FinancialCutConflict("journal event slot sequence mismatch")
    return event


@dataclass(frozen=True, slots=True)
class ScientificFinancialCut:
    """Content identity for one scientific subject and one financial truth cut."""

    scientific_protocol_id: str
    gate_profile_digest: str
    provider_id: str
    account_id: str
    environment: str
    provider_environment: str
    reconciliation_event_id: str
    reconciliation_journal_sequence: int
    journal_sequence: int
    journal_population_digest: str
    reconciliation_checkpoint_digest: str
    cut_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self) is not ScientificFinancialCut:
            raise TypeError("financial cut must be exact ScientificFinancialCut")
        for field_name in (
            "scientific_protocol_id",
            "account_id",
            "reconciliation_event_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), name=field_name),
            )
        object.__setattr__(
            self,
            "provider_id",
            _text(self.provider_id, name="provider_id").upper(),
        )
        object.__setattr__(
            self,
            "environment",
            _text(self.environment, name="environment").upper(),
        )
        object.__setattr__(
            self,
            "provider_environment",
            normalize_provider_environment(
                provider_id=self.provider_id,
                environment=self.environment,
                provider_environment=self.provider_environment,
            ),
        )
        for field_name in (
            "gate_profile_digest",
            "journal_population_digest",
            "reconciliation_checkpoint_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _sha256(getattr(self, field_name), name=field_name),
            )
        if (
            type(self.reconciliation_journal_sequence) is not int
            or self.reconciliation_journal_sequence <= 0
        ):
            raise ValueError(
                "reconciliation_journal_sequence must be a positive integer"
            )
        if type(self.journal_sequence) is not int or self.journal_sequence < 0:
            raise ValueError("journal_sequence must be a non-negative integer")
        if self.reconciliation_journal_sequence > self.journal_sequence:
            raise ValueError(
                "reconciliation_journal_sequence cannot exceed journal_sequence"
            )
        subject = {
            "schema": "autotrade.scientific-financial-cut.v3",
            "scientific_protocol_id": self.scientific_protocol_id,
            "gate_profile_digest": self.gate_profile_digest,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "provider_environment": self.provider_environment,
            "reconciliation_event_id": self.reconciliation_event_id,
            "reconciliation_journal_sequence": self.reconciliation_journal_sequence,
            "journal_sequence": self.journal_sequence,
            "journal_population_digest": self.journal_population_digest,
            "reconciliation_checkpoint_digest": self.reconciliation_checkpoint_digest,
        }
        object.__setattr__(self, "cut_digest", payload_digest(subject))


def capture_current_scientific_financial_cut(
    store: JournalStore,
    *,
    scientific_protocol_id: str,
    gate_profile_digest: str,
    provider_id: str,
    account_id: str,
    environment: str,
    reconciliation_event_id: str,
    provider_environment: str | None = None,
) -> ScientificFinancialCut:
    """Capture one stable exact journal population plus current reconciliation.

    One existing JournalStore remains the authority. The function freezes its
    current sequence, hashes every append-only event through that sequence,
    requires the selected reconciliation checkpoint to be current for the same
    provider/account/environment, then re-reads the whole-store cut. Any write
    racing the capture invalidates the evidence rather than producing a mixed
    scientific/financial snapshot.
    """

    protocol_id = _text(scientific_protocol_id, name="scientific_protocol_id")
    profile_digest = _sha256(gate_profile_digest, name="gate_profile_digest")
    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    env = _text(environment, name="environment").upper()
    provider_env = normalize_provider_environment(
        provider_id=provider,
        environment=env,
        provider_environment=provider_environment,
    )
    checkpoint_id = _text(
        reconciliation_event_id,
        name="reconciliation_event_id",
    )

    identity = require_exact_journal_store_authority(
        store,
        subject="scientific financial JournalStore",
    )
    with journal_store_authority_scope(store, identity):
        before = _exact_dict(
            JournalStore.whole_store_state_cut(store),
            name="journal_state_before",
        )
        frozen_sequence = _journal_sequence(before)
        population_digest = _digest_exact_journal_population(
            store,
            target_sequence=frozen_sequence,
        )
        latest = None
        latest_error: Exception | None = None
        try:
            latest = load_latest_reconciliation_checkpoint_for_scope(
                store,
                provider_id=provider,
                account_id=account,
                environment=env,
                provider_environment=provider_env,
            )
        except (TypeError, ValueError) as error:
            latest_error = error

        checkpoint = None
        checkpoint_error: Exception | None = None
        checkpoint_content_error: FinancialCutConflict | None = None
        reconciliation_sequence: int | None = None
        if latest_error is None and latest is not None:
            try:
                checkpoint = require_current_reconciliation_checkpoint(
                    store,
                    checkpoint_event_id=checkpoint_id,
                    provider_id=provider,
                    account_id=account,
                    environment=env,
                    provider_environment=provider_env,
                )
            except (TypeError, ValueError) as error:
                checkpoint_error = error
            else:
                try:
                    checkpoint = _exact_dict(
                        checkpoint,
                        name="reconciliation_checkpoint",
                    )
                    reconciliation_sequence = checkpoint.get("journal_sequence")
                    if (
                        type(reconciliation_sequence) is not int
                        or reconciliation_sequence <= 0
                    ):
                        raise FinancialCutConflict(
                            "reconciliation checkpoint lacks durable journal sequence"
                        )
                    if reconciliation_sequence > frozen_sequence:
                        raise FinancialCutConflict(
                            "reconciliation checkpoint crossed the frozen financial cut"
                        )
                    population_checkpoint = _event_at_journal_sequence(
                        store,
                        journal_sequence=reconciliation_sequence,
                    )
                    if population_checkpoint != checkpoint:
                        raise FinancialCutConflict(
                            "reconciliation checkpoint does not match the frozen journal population"
                        )
                except FinancialCutConflict as error:
                    checkpoint_content_error = error
        after = _exact_dict(
            JournalStore.whole_store_state_cut(store),
            name="journal_state_after",
        )

    if before != after or _journal_sequence(after) != frozen_sequence:
        raise FinancialCutConflict(
            "financial journal changed while the scientific financial cut was captured"
        )
    if latest_error is not None:
        raise FinancialCutConflict(
            "reconciliation checkpoint history is not authoritative for the requested scope"
        ) from latest_error
    if latest is None:
        raise FinancialCutUnavailable(
            "current reconciliation checkpoint is unavailable"
        )
    if checkpoint_error is not None:
        raise FinancialCutConflict(
            "reconciliation checkpoint is not authoritative for the requested scope"
        ) from checkpoint_error
    if checkpoint_content_error is not None:
        raise checkpoint_content_error
    if checkpoint is None or reconciliation_sequence is None:
        raise FinancialCutConflict(
            "reconciliation checkpoint authority resolution was incomplete"
        )

    reconciliation_digest = payload_digest(checkpoint)
    return ScientificFinancialCut(
        scientific_protocol_id=protocol_id,
        gate_profile_digest=profile_digest,
        provider_id=provider,
        account_id=account,
        environment=env,
        provider_environment=provider_env,
        reconciliation_event_id=checkpoint_id,
        reconciliation_journal_sequence=reconciliation_sequence,
        journal_sequence=frozen_sequence,
        journal_population_digest=population_digest,
        reconciliation_checkpoint_digest=reconciliation_digest,
    )
