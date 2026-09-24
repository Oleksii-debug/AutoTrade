"""Fail-closed runtime recovery control for AutoTrade simulation foundations.

This module models recovery invariants only.  It does not perform live trading.
It deliberately refuses automatic sender failover on lease expiry, preserves
UNKNOWN send outcomes until reconciliation, and prevents false READY states.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class HostState(str, Enum):
    STOPPED = "STOPPED"
    RECOVERING = "RECOVERING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


class SendPhase(str, Enum):
    CREATED = "CREATED"
    DURABLE = "DURABLE"
    SENT_UNKNOWN = "SENT_UNKNOWN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    PROVEN_ABSENT = "PROVEN_ABSENT"


@dataclass(frozen=True)
class OwnerFence:
    owner_id: str
    epoch: int


@dataclass
class OutboundAttempt:
    attempt_id: str
    intent_id: str
    owner_epoch: int
    phase: SendPhase = SendPhase.CREATED
    provider_order_id: str | None = None
    evidence: list[str] = field(default_factory=list)

    def persist(self) -> None:
        if self.phase is not SendPhase.CREATED:
            raise ValueError("Attempt can be persisted only once")
        self.phase = SendPhase.DURABLE

    def mark_send_started(self, evidence_ref: str) -> None:
        if self.phase is not SendPhase.DURABLE:
            raise ValueError("Send requires a durable attempt")
        if not evidence_ref:
            raise ValueError("Send evidence is required")
        self.phase = SendPhase.SENT_UNKNOWN
        self.evidence.append(evidence_ref)

    def acknowledge(self, provider_order_id: str, evidence_ref: str) -> None:
        if self.phase is not SendPhase.SENT_UNKNOWN:
            raise ValueError("Acknowledgement requires an uncertain sent attempt")
        if not provider_order_id or not evidence_ref:
            raise ValueError("Provider order and evidence are required")
        self.phase = SendPhase.ACKNOWLEDGED
        self.provider_order_id = provider_order_id
        self.evidence.append(evidence_ref)

    def reject(self, evidence_ref: str) -> None:
        if self.phase is not SendPhase.SENT_UNKNOWN:
            raise ValueError("Rejection requires an uncertain sent attempt")
        if not evidence_ref:
            raise ValueError("Rejection evidence is required")
        self.phase = SendPhase.REJECTED
        self.evidence.append(evidence_ref)

    def prove_absent(self, evidence_refs: Iterable[str]) -> None:
        if self.phase is not SendPhase.SENT_UNKNOWN:
            raise ValueError("Absence proof requires an uncertain sent attempt")
        refs = [item for item in evidence_refs if item]
        if len(refs) < 2:
            raise ValueError("Absence proof requires independent evidence")
        self.phase = SendPhase.PROVEN_ABSENT
        self.evidence.extend(refs)

    @property
    def retry_disposition(self) -> str:
        if self.phase in {SendPhase.CREATED, SendPhase.DURABLE, SendPhase.PROVEN_ABSENT}:
            return "SAFE_WITH_NEW_ADMISSION"
        if self.phase is SendPhase.SENT_UNKNOWN:
            return "RECONCILE_FIRST"
        return "NEVER"


class RecoveryController:
    """Tracks sender ownership, host readiness and unresolved external truth."""

    def __init__(self) -> None:
        self.state = HostState.STOPPED
        self.owner: OwnerFence | None = None
        self.reason_codes: set[str] = set()
        self.unresolved_attempts: set[str] = set()
        self.storage_writable = True
        self.clock_trusted = True
        self.provider_reconciled = False
        self.market_data_fresh = True
        self.runtime_overloaded = False
        self.last_financial_event_sequence: int | None = None

    def start(self, owner_id: str) -> OwnerFence:
        if not owner_id:
            raise ValueError("Owner identity is required")
        if self.owner is not None:
            raise RuntimeError("Host already has an owner")
        self.owner = OwnerFence(owner_id=owner_id, epoch=1)
        self.state = HostState.RECOVERING
        self.provider_reconciled = False
        self.reason_codes = {"startup_reconciliation_required"}
        return self.owner

    def record_reconciliation(self, *, consistent: bool, uncertainty: Iterable[str] = ()) -> None:
        if self.owner is None:
            raise RuntimeError("No active owner")
        unresolved = {item for item in uncertainty if item}
        self.unresolved_attempts = unresolved
        self.provider_reconciled = bool(consistent and not unresolved)
        if self.provider_reconciled:
            self.reason_codes.discard("startup_reconciliation_required")
            self.reason_codes.discard("provider_uncertainty")
            self.reason_codes.discard("financial_event_gap")
        else:
            self.reason_codes.add("provider_uncertainty")
        self._recompute_state()

    def set_storage_writable(self, writable: bool) -> None:
        self.storage_writable = writable
        if writable:
            self.reason_codes.discard("durable_journal_unavailable")
        else:
            self.reason_codes.add("durable_journal_unavailable")
        self._recompute_state()

    def set_clock_trusted(self, trusted: bool) -> None:
        self.clock_trusted = trusted
        if trusted:
            self.reason_codes.discard("clock_untrusted")
        else:
            self.reason_codes.add("clock_untrusted")
        self._recompute_state()

    def set_market_data_fresh(self, fresh: bool) -> None:
        self.market_data_fresh = bool(fresh)
        if self.market_data_fresh:
            self.reason_codes.discard("market_data_stale")
        else:
            self.reason_codes.add("market_data_stale")
        self._recompute_state()

    def set_runtime_overloaded(self, overloaded: bool) -> None:
        self.runtime_overloaded = bool(overloaded)
        if self.runtime_overloaded:
            self.reason_codes.add("runtime_overload")
        else:
            self.reason_codes.discard("runtime_overload")
        self._recompute_state()

    def observe_financial_event_sequence(self, sequence: int) -> bool:
        """Record a provider financial-event sequence without hiding gaps.

        Duplicate delivery of the current sequence is idempotent. A gap or
        backward sequence invalidates reconciliation and remains visible until a
        fresh provider reconciliation is recorded.
        """
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError("Financial event sequence must be a non-negative integer")
        prior = self.last_financial_event_sequence
        if prior is None:
            self.last_financial_event_sequence = sequence
            return True
        if sequence == prior:
            return False
        if sequence != prior + 1:
            self.provider_reconciled = False
            self.reason_codes.add("financial_event_gap")
            self._recompute_state()
            return False
        self.last_financial_event_sequence = sequence
        return True

    def note_unknown_send(self, attempt: OutboundAttempt) -> None:
        if attempt.phase is not SendPhase.SENT_UNKNOWN:
            raise ValueError("Only uncertain sent attempts block readiness")
        self.unresolved_attempts.add(attempt.attempt_id)
        self.provider_reconciled = False
        self.reason_codes.add("provider_uncertainty")
        self._recompute_state()

    def resolve_attempt(self, attempt: OutboundAttempt) -> None:
        if attempt.phase not in {
            SendPhase.ACKNOWLEDGED,
            SendPhase.REJECTED,
            SendPhase.PROVEN_ABSENT,
        }:
            raise ValueError("Attempt is not externally resolved")
        self.unresolved_attempts.discard(attempt.attempt_id)
        if not self.unresolved_attempts:
            self.reason_codes.discard("provider_uncertainty")
        self._recompute_state()

    def transfer_owner(
        self,
        *,
        new_owner_id: str,
        old_sender_fenced: bool,
        reconciled: bool,
    ) -> OwnerFence:
        if self.owner is None:
            raise RuntimeError("No current owner to transfer")
        if not new_owner_id:
            raise ValueError("New owner identity is required")
        if new_owner_id == self.owner.owner_id:
            raise ValueError("New owner must differ from current owner")
        if not old_sender_fenced:
            raise PermissionError("Old sender must be externally fenced")
        if not reconciled or self.unresolved_attempts:
            raise PermissionError("Ownership transfer requires reconciliation")
        self.owner = OwnerFence(new_owner_id, self.owner.epoch + 1)
        self.provider_reconciled = False
        self.reason_codes.discard("lease_expired_no_failover")
        self.reason_codes.add("startup_reconciliation_required")
        self.state = HostState.RECOVERING
        return self.owner

    def _soft_pressure_only(self) -> bool:
        soft = {"market_data_stale", "runtime_overload"}
        return (
            self.state is HostState.DEGRADED
            and bool(self.reason_codes)
            and self.reason_codes <= soft
            and self.provider_reconciled
            and not self.unresolved_attempts
            and self.storage_writable
            and self.clock_trusted
        )

    def validate_sender(
        self,
        owner_id: str,
        owner_epoch: int,
        *,
        risk_reducing: bool = False,
    ) -> None:
        if self.owner is None:
            raise PermissionError("No active sender")
        if owner_id != self.owner.owner_id or owner_epoch != self.owner.epoch:
            raise PermissionError("Sender fence mismatch")
        if self.state is HostState.READY:
            return
        if risk_reducing and self._soft_pressure_only():
            return
        raise PermissionError("Host is not ready for new sends")

    def validate_admission(
        self,
        owner_epoch: int,
        *,
        risk_reducing: bool = False,
    ) -> None:
        if self.owner is None or owner_epoch != self.owner.epoch:
            raise PermissionError("Admission owner epoch is stale")
        if not self.storage_writable:
            raise PermissionError("Durable journal is unavailable")
        if not self.clock_trusted:
            raise PermissionError("Clock is not trusted")
        if self.unresolved_attempts:
            raise PermissionError("External uncertainty is unresolved")
        if self.state is HostState.READY:
            return
        if risk_reducing and self._soft_pressure_only():
            return
        raise PermissionError("Host is not ready")

    def on_lease_expired(self) -> None:
        """Lease expiry never transfers sender authority by itself."""
        if self.owner is None:
            return
        self.reason_codes.add("lease_expired_no_failover")
        if self.state is HostState.READY:
            self.state = HostState.DEGRADED

    def stop(self) -> None:
        self.state = HostState.STOPPED
        self.owner = None
        self.provider_reconciled = False
        self.reason_codes = {"stopped"}

    def _recompute_state(self) -> None:
        if self.owner is None:
            self.state = HostState.STOPPED
            return
        if not self.storage_writable:
            self.state = HostState.BLOCKED
            return
        if not self.clock_trusted:
            self.state = HostState.BLOCKED
            return
        if self.unresolved_attempts:
            self.state = HostState.DEGRADED
            return
        if "lease_expired_no_failover" in self.reason_codes:
            self.state = HostState.DEGRADED
            return
        if "financial_event_gap" in self.reason_codes:
            self.state = HostState.DEGRADED
            return
        if not self.market_data_fresh or self.runtime_overloaded:
            self.state = HostState.DEGRADED
            return
        if self.provider_reconciled and "startup_reconciliation_required" not in self.reason_codes:
            self.state = HostState.READY
        else:
            self.state = HostState.RECOVERING
