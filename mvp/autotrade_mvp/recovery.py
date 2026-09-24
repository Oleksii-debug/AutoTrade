"""Fail-closed runtime recovery control for AutoTrade simulation foundations.

This module models recovery invariants only.  It does not perform live trading.
It deliberately refuses automatic sender failover on lease expiry, preserves
UNKNOWN send outcomes until reconciliation, and prevents false READY states.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable
from uuid import NAMESPACE_URL, uuid5

from .persistence import JournalStore, payload_digest


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
    """Tracks sender ownership, host readiness and unresolved external truth.

    With owner_store supplied, owner epochs are journal-backed. Every new
    process/start appends the next monotonic epoch before it can reconcile to
    READY, and sender/admission validation rereads that shared durable fence.
    This prevents a restart from silently reusing epoch 1.
    """

    _OWNER_AGGREGATE_TYPE = "recovery_owner"
    _OWNER_EVENT_TYPE = "RecoveryOwnerChanged"

    def __init__(
        self,
        *,
        owner_store: JournalStore | None = None,
        owner_scope: str = "default",
    ) -> None:
        if owner_store is not None and not isinstance(owner_store, JournalStore):
            raise TypeError("owner_store must be JournalStore or None")
        if not isinstance(owner_scope, str) or not owner_scope.strip():
            raise ValueError("owner_scope is required")
        self._owner_store = owner_store
        self._owner_scope = owner_scope.strip()
        self.state = HostState.STOPPED
        self.owner: OwnerFence | None = None
        self.reason_codes: set[str] = set()
        self.unresolved_attempts: set[str] = set()
        self.storage_writable = True
        self.clock_trusted = True
        self.provider_reconciled = False

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _latest_durable_owner(self) -> OwnerFence | None:
        if self._owner_store is None:
            return None
        events = self._owner_store.load_events(
            self._OWNER_AGGREGATE_TYPE,
            self._owner_scope,
        )
        if not events:
            return None
        latest = events[-1]
        if latest["event_type"] != self._OWNER_EVENT_TYPE:
            raise RuntimeError("Recovery owner journal contains unsupported event type")
        payload = latest["payload"]
        if not isinstance(payload, dict):
            raise RuntimeError("Recovery owner journal payload must be an object")
        if payload_digest(payload) != latest["payload_hash"]:
            raise RuntimeError("Recovery owner journal payload hash mismatch")
        owner_id = payload.get("owner_id")
        epoch_raw = payload.get("owner_epoch")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise RuntimeError("Recovery owner journal contains invalid owner identity")
        if (
            not isinstance(epoch_raw, str)
            or not epoch_raw.isdigit()
            or epoch_raw == "0"
            or (len(epoch_raw) > 1 and epoch_raw.startswith("0"))
        ):
            raise RuntimeError("Recovery owner journal contains invalid owner epoch")
        epoch = int(epoch_raw)
        if int(latest["aggregate_version"]) != epoch:
            raise RuntimeError("Recovery owner journal epoch/version mismatch")
        return OwnerFence(owner_id=owner_id.strip(), epoch=epoch)

    def _append_durable_owner(self, owner: OwnerFence) -> None:
        if self._owner_store is None:
            return
        payload = {
            "owner_id": owner.owner_id,
            "owner_epoch": str(owner.epoch),
        }
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://recovery.autotrade.local/"
                f"{self._owner_scope!r}/{owner.epoch}/{owner.owner_id!r}",
            )
        )
        self._owner_store.append_event(
            {
                "event_id": event_id,
                "event_type": self._OWNER_EVENT_TYPE,
                "aggregate_type": self._OWNER_AGGREGATE_TYPE,
                "aggregate_id": self._owner_scope,
                "aggregate_version": str(owner.epoch),
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": self._now(),
            }
        )

    def _require_current_durable_owner(self) -> None:
        if self._owner_store is None or self.owner is None:
            return
        durable = self._latest_durable_owner()
        if durable != self.owner:
            raise PermissionError("Durable sender fence no longer belongs to this owner")

    def start(self, owner_id: str) -> OwnerFence:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("Owner identity is required")
        if self.owner is not None:
            raise RuntimeError("Host already has an owner")
        normalized_owner = owner_id.strip()
        durable = self._latest_durable_owner()
        next_epoch = 1 if durable is None else durable.epoch + 1
        candidate = OwnerFence(owner_id=normalized_owner, epoch=next_epoch)
        self._append_durable_owner(candidate)
        self.owner = candidate
        self.state = HostState.RECOVERING
        self.provider_reconciled = False
        self.reason_codes = {"startup_reconciliation_required"}
        return self.owner

    def record_reconciliation(self, *, consistent: bool, uncertainty: Iterable[str] = ()) -> None:
        if type(consistent) is not bool:
            raise TypeError("consistent must be boolean")
        if self.owner is None:
            raise RuntimeError("No active owner")
        self._require_current_durable_owner()
        unresolved = {item for item in uncertainty if item}
        self.unresolved_attempts = unresolved
        self.provider_reconciled = bool(consistent and not unresolved)
        if self.provider_reconciled:
            self.reason_codes.discard("startup_reconciliation_required")
            self.reason_codes.discard("provider_uncertainty")
        else:
            self.reason_codes.add("provider_uncertainty")
        self._recompute_state()

    def set_storage_writable(self, writable: bool) -> None:
        if type(writable) is not bool:
            raise TypeError("writable must be boolean")
        self.storage_writable = writable
        if writable:
            self.reason_codes.discard("durable_journal_unavailable")
        else:
            self.reason_codes.add("durable_journal_unavailable")
        self._recompute_state()

    def set_clock_trusted(self, trusted: bool) -> None:
        if type(trusted) is not bool:
            raise TypeError("trusted must be boolean")
        self.clock_trusted = trusted
        if trusted:
            self.reason_codes.discard("clock_untrusted")
        else:
            self.reason_codes.add("clock_untrusted")
        self._recompute_state()

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
        if not isinstance(new_owner_id, str) or not new_owner_id.strip():
            raise ValueError("New owner identity is required")
        normalized_owner = new_owner_id.strip()
        if normalized_owner == self.owner.owner_id:
            raise ValueError("New owner must differ from current owner")
        if type(old_sender_fenced) is not bool:
            raise TypeError("old_sender_fenced must be boolean")
        if type(reconciled) is not bool:
            raise TypeError("reconciled must be boolean")
        if not old_sender_fenced:
            raise PermissionError("Old sender must be externally fenced")
        if not reconciled or self.unresolved_attempts:
            raise PermissionError("Ownership transfer requires reconciliation")
        self._require_current_durable_owner()
        candidate = OwnerFence(normalized_owner, self.owner.epoch + 1)
        self._append_durable_owner(candidate)
        self.owner = candidate
        self.provider_reconciled = False
        self.reason_codes.discard("lease_expired_no_failover")
        self.reason_codes.add("startup_reconciliation_required")
        self.state = HostState.RECOVERING
        return self.owner

    def validate_sender(self, owner_id: str, owner_epoch: int) -> None:
        if not isinstance(owner_epoch, int) or isinstance(owner_epoch, bool) or owner_epoch < 1:
            raise ValueError("owner_epoch must be a positive integer")
        if self.owner is None:
            raise PermissionError("No active sender")
        self._require_current_durable_owner()
        if owner_id != self.owner.owner_id or owner_epoch != self.owner.epoch:
            raise PermissionError("Sender fence mismatch")
        if self.state is not HostState.READY:
            raise PermissionError("Host is not ready for new sends")

    def validate_admission(self, owner_epoch: int) -> None:
        if not isinstance(owner_epoch, int) or isinstance(owner_epoch, bool) or owner_epoch < 1:
            raise ValueError("owner_epoch must be a positive integer")
        if self.owner is not None:
            self._require_current_durable_owner()
        if self.owner is None or owner_epoch != self.owner.epoch:
            raise PermissionError("Admission owner epoch is stale")
        if self.state is not HostState.READY:
            raise PermissionError("Host is not ready")
        if not self.storage_writable:
            raise PermissionError("Durable journal is unavailable")
        if not self.clock_trusted:
            raise PermissionError("Clock is not trusted")
        if self.unresolved_attempts:
            raise PermissionError("External uncertainty is unresolved")

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
        if self.provider_reconciled and "startup_reconciliation_required" not in self.reason_codes:
            self.state = HostState.READY
        else:
            self.state = HostState.RECOVERING
