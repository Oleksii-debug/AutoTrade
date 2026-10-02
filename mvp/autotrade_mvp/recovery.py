"""Fail-closed runtime recovery control for AutoTrade simulation foundations.

This module models recovery invariants only.  It does not perform live trading.
It deliberately refuses automatic sender failover on lease expiry, preserves
UNKNOWN send outcomes until reconciliation, and prevents false READY states.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable
from uuid import NAMESPACE_URL, uuid5

from .dispatch import (
    GuardedDispatcher,
    _issue_recovery_guarded_dispatcher,
    submission_attempt_aggregate_id,
)
from .persistence import (
    JournalStore,
    payload_digest,
    require_exact_journal_store_authority,
)
from .sender_gate import journal_sender_gate
from .store_identity import (
    JournalStoreIdentity,
    require_exact_journal_store_identity,
)
from .reconciliation_journal import (
    load_reconciliation_checkpoint_for_readiness,
    load_submission_resolution_evidence,
)


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

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id.strip():
            raise ValueError("Attempt identity is required")
        if not isinstance(self.intent_id, str) or not self.intent_id.strip():
            raise ValueError("Intent identity is required")
        if (
            not isinstance(self.owner_epoch, int)
            or isinstance(self.owner_epoch, bool)
            or self.owner_epoch <= 0
        ):
            raise ValueError("Owner epoch must be a positive integer")
        self.attempt_id = self.attempt_id.strip()
        self.intent_id = self.intent_id.strip()
        if not isinstance(self.phase, SendPhase):
            raise TypeError("Attempt phase must be a SendPhase")
        if self.phase is not SendPhase.CREATED:
            raise ValueError("New attempt must begin in CREATED phase")
        if self.provider_order_id is not None:
            raise ValueError("New attempt cannot already have provider order identity")
        if self.evidence:
            raise ValueError("New attempt cannot already contain evidence")

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
        refs: list[str] = []
        for item in evidence_refs:
            if not isinstance(item, str) or not item:
                raise ValueError("Absence proof evidence refs must be non-empty strings")
            refs.append(item)
        if len(refs) < 2 or len(set(refs)) != len(refs):
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
    This prevents a restart from silently reusing epoch 1 while preserving
    the newer storage/clock/UNKNOWN recovery semantics.
    """

    _OWNER_AGGREGATE_TYPE = "recovery_owner"
    _OWNER_EVENT_TYPE = "RecoveryOwnerChanged"

    def __init__(
        self,
        *,
        owner_store: JournalStore | None = None,
        owner_scope: str = "default",
    ) -> None:
        if not isinstance(owner_scope, str) or not owner_scope.strip():
            raise ValueError("owner_scope is required")
        self._owner_store = owner_store
        self._owner_store_identity: JournalStoreIdentity | None = (
            None
            if owner_store is None
            else require_exact_journal_store_authority(
                owner_store,
                subject="recovery owner JournalStore",
            )
        )
        self._owner_scope = owner_scope.strip()
        self.state = HostState.STOPPED
        self.owner: OwnerFence | None = None
        self.reason_codes: set[str] = set()
        self.unresolved_attempts: set[str] = set()
        self._unresolved_send_attempts: set[str] = set()
        self._unresolved_send_bindings: dict[
            str,
            tuple[str, int, tuple[str, ...]],
        ] = {}
        self._recovered_unknown_identities: dict[
            str,
            tuple[str, str, str, str, str],
        ] = {}
        self.storage_writable = True
        self.clock_trusted = True
        self.provider_reconciled = False

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @property
    def owner_scope(self) -> str:
        """Canonical durable owner scope used by this recovery controller."""

        return self._owner_scope

    def _selected_journal_identity(self) -> JournalStoreIdentity:
        identity = self._owner_store_identity
        if identity is None:
            raise PermissionError("Recovery controller has no durable journal authority")
        return require_exact_journal_store_identity(
            identity,
            subject="selected recovery journal identity",
        )

    def _journal_store_authority(self) -> JournalStore:
        if self._owner_store is None:
            raise PermissionError("Recovery controller has no durable journal authority")
        expected = self._selected_journal_identity()
        identity = require_exact_journal_store_authority(self._owner_store)
        if identity != expected:
            raise PermissionError("recovery journal authority changed")
        return self._owner_store

    @property
    def durable_owner_store_path(self) -> Path | None:
        """Return the selected journal path backing sender fencing, if durable."""

        if self._owner_store is None:
            return None
        self._journal_store_authority()
        return Path(self._selected_journal_identity().canonical_path)

    def durable_owner_chain(self) -> tuple[OwnerFence, ...]:
        """Read and validate the complete monotonic sender-fence chain.

        This is read-only evidence access.  It never grants readiness or
        changes sender ownership.
        """

        if self._owner_store is None:
            return ()
        store = self._journal_store_authority()
        events = JournalStore.load_events(
            store,
            self._OWNER_AGGREGATE_TYPE,
            self._owner_scope,
        )
        chain: list[OwnerFence] = []
        for expected_epoch, event in enumerate(events, start=1):
            if event["event_type"] != self._OWNER_EVENT_TYPE:
                raise RuntimeError(
                    "Recovery owner journal contains unsupported event type"
                )
            payload = event["payload"]
            if not isinstance(payload, dict):
                raise RuntimeError(
                    "Recovery owner journal payload must be an object"
                )
            if payload_digest(payload) != event["payload_hash"]:
                raise RuntimeError(
                    "Recovery owner journal payload hash mismatch"
                )
            owner_id = payload.get("owner_id")
            epoch_raw = payload.get("owner_epoch")
            if not isinstance(owner_id, str) or not owner_id.strip():
                raise RuntimeError(
                    "Recovery owner journal contains invalid owner identity"
                )
            if (
                not isinstance(epoch_raw, str)
                or not epoch_raw.isdigit()
                or epoch_raw == "0"
                or (len(epoch_raw) > 1 and epoch_raw.startswith("0"))
            ):
                raise RuntimeError(
                    "Recovery owner journal contains invalid owner epoch"
                )
            epoch = int(epoch_raw)
            if (
                int(event["aggregate_version"]) != expected_epoch
                or epoch != expected_epoch
            ):
                raise RuntimeError(
                    "Recovery owner journal epoch/version chain is invalid"
                )
            chain.append(OwnerFence(owner_id=owner_id.strip(), epoch=epoch))
        return tuple(chain)

    def _latest_durable_owner(self) -> OwnerFence | None:
        chain = self.durable_owner_chain()
        return chain[-1] if chain else None

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
        store = self._journal_store_authority()
        JournalStore.append_event(
            store,
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
        with journal_sender_gate(self._owner_store):
            durable = self._latest_durable_owner()
            if self._owner_store is not None and durable is not None:
                raise PermissionError(
                    "Durable owner already exists; independently authorized takeover is required"
                )
            next_epoch = 1 if durable is None else durable.epoch + 1
            candidate = OwnerFence(owner_id=normalized_owner, epoch=next_epoch)
            self._append_durable_owner(candidate)
            self.owner = candidate
            self.state = HostState.RECOVERING
            self.provider_reconciled = False
            self.reason_codes = {"startup_reconciliation_required"}
            self._recover_scoped_submission_uncertainty_from_owner_scope()
            return self.owner

    @staticmethod
    def _normalized_submission_scope(
        environment: str,
        account_id: str,
    ) -> tuple[str, str]:
        normalized_environment = (
            environment.strip().upper() if isinstance(environment, str) else ""
        )
        if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
            raise ValueError(
                "environment must be REPLAY, SIMULATION, PAPER, or LIVE"
            )
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("account_id is required")
        return normalized_environment, account_id.strip()

    def _recover_scoped_submission_uncertainty_from_owner_scope(self) -> None:
        """Restore durable UNKNOWN send barriers for ENVIRONMENT:account scopes.

        Sender ownership is durable already.  This companion scan reconstructs
        ambiguous SubmissionSending/SubmissionUnknown state from the same
        journal after process restart, before READY can be established.
        """

        if self._owner_store is None or ":" not in self._owner_scope:
            return
        environment, account_id = self._owner_scope.split(":", 1)
        if environment.strip().upper() not in {
            "REPLAY", "SIMULATION", "PAPER", "LIVE"
        }:
            return
        self.recover_durable_submission_uncertainty(
            environment=environment,
            account_id=account_id,
        )

    @staticmethod
    def _validated_submission_event_sequence(
        aggregate_id: str,
        aggregate_events: list[dict[str, object]],
    ) -> tuple[str, ...]:
        """Validate one durable submission attempt before recovery classifies it.

        JournalStore proves byte integrity; recovery must additionally prove both
        the dispatch state machine and one continuous durable sender identity.
        Prepared is the canonical sender/client identity for the attempt.
        """

        if not aggregate_events:
            raise RuntimeError("Submission journal aggregate is empty")
        event_types: list[str] = []
        for expected_version, event in enumerate(aggregate_events, start=1):
            if event.get("aggregate_id") != aggregate_id:
                raise RuntimeError("Submission journal aggregate identity changed")
            version = event.get("aggregate_version")
            if type(version) is not int or version != expected_version:
                raise RuntimeError(
                    "Submission journal aggregate versions are not contiguous"
                )
            event_type = event.get("event_type")
            if not isinstance(event_type, str) or not event_type:
                raise RuntimeError("Submission journal event type is invalid")
            event_types.append(event_type)

        sequence = tuple(event_types)
        valid_sequences = {
            ("SubmissionPrepared",),
            ("SubmissionPrepared", "SubmissionBlocked"),
            ("SubmissionPrepared", "SubmissionUnknown"),
            ("SubmissionPrepared", "SubmissionSending"),
            (
                "SubmissionPrepared",
                "SubmissionSending",
                "SubmissionSent",
            ),
            (
                "SubmissionPrepared",
                "SubmissionSending",
                "SubmissionUnknown",
            ),
            # A provider wrapper may swallow/mask a final-guard rejection.
            # The dispatcher records Blocked first, then upgrades the outcome
            # to UNKNOWN because an outbound side effect can no longer be
            # disproved. Recovery must preserve that legitimate ambiguity.
            (
                "SubmissionPrepared",
                "SubmissionBlocked",
                "SubmissionUnknown",
            ),
        }
        if sequence not in valid_sequences:
            raise RuntimeError(
                "Submission journal transition sequence is invalid: "
                + " -> ".join(sequence)
            )

        prepared = aggregate_events[0]
        prepared_payload = prepared.get("payload")
        if not isinstance(prepared_payload, dict):
            raise RuntimeError("SubmissionPrepared payload is invalid")
        prepared_owner_token = prepared_payload.get("owner_token")
        prepared_owner_epoch = prepared_payload.get("owner_epoch")
        prepared_client_order_id = prepared_payload.get("client_order_id")
        if (
            not isinstance(prepared_owner_token, str)
            or not prepared_owner_token.strip()
        ):
            raise RuntimeError("SubmissionPrepared owner token is invalid")
        if (
            type(prepared_owner_epoch) is not int
            or prepared_owner_epoch <= 0
        ):
            raise RuntimeError("SubmissionPrepared owner epoch is invalid")
        if (
            not isinstance(prepared_client_order_id, str)
            or not prepared_client_order_id.strip()
        ):
            raise RuntimeError("SubmissionPrepared client order identity is invalid")

        expected_envelope_epoch = str(prepared_owner_epoch)
        for event in aggregate_events:
            envelope_epoch = event.get("owner_epoch")
            if (
                not isinstance(envelope_epoch, str)
                or not envelope_epoch.isdigit()
                or envelope_epoch == "0"
                or (
                    len(envelope_epoch) > 1
                    and envelope_epoch.startswith("0")
                )
            ):
                raise RuntimeError("Submission journal owner epoch is invalid")
            if envelope_epoch != expected_envelope_epoch:
                raise RuntimeError(
                    "Submission journal sender owner epoch changed"
                )

            event_payload = event.get("payload")
            if not isinstance(event_payload, dict):
                raise RuntimeError("Submission journal event payload is invalid")
            if event is prepared:
                if envelope_epoch != str(prepared_owner_epoch):
                    raise RuntimeError(
                        "SubmissionPrepared envelope/payload owner epoch mismatch"
                    )
                continue

            if event_payload.get("client_order_id") != prepared_client_order_id:
                raise RuntimeError(
                    "Submission journal client order identity changed"
                )
            if (
                "owner_epoch" in event_payload
                and event_payload.get("owner_epoch") != prepared_owner_epoch
            ):
                raise RuntimeError(
                    "Submission journal payload owner epoch changed"
                )
            if (
                "owner_token" in event_payload
                and event_payload.get("owner_token") != prepared_owner_token
            ):
                raise RuntimeError(
                    "Submission journal payload owner token changed"
                )
            if event.get("event_type") == "SubmissionSending":
                if (
                    event_payload.get("owner_epoch") != prepared_owner_epoch
                    or event_payload.get("owner_token") != prepared_owner_token
                ):
                    raise RuntimeError(
                        "SubmissionSending sender identity does not match Prepared"
                    )

        return sequence

    def _durable_terminal_resolution_dominates_submission(
        self,
        *,
        store: JournalStore,
        ambiguous_event: dict[str, object],
        attempt_id: str,
        intent_id: str,
        client_order_id: str,
        provider_id: str,
        environment: str,
        account_id: str,
    ) -> bool:
        """Project one older ambiguous tail against later durable provider truth.

        Durable account checkpoints are scope heads for current readiness, but a
        later full checkpoint is not required to repeat every historically
        terminal submission verdict.  For this one attempt, scan the canonical
        reconciliation chronology and select the latest *explicit* current-owner
        resolution after the possible-send cut.  Omission is not revocation; a
        later explicit UNKNOWN is.
        """

        if self.owner is None:
            return False
        ambiguous_sequence = ambiguous_event.get("journal_sequence")
        if type(ambiguous_sequence) is not int or ambiguous_sequence <= 0:
            raise RuntimeError(
                "Submission durable ordering identity is invalid"
            )

        normalized_provider = provider_id.strip().upper()
        normalized_account = account_id.strip()
        normalized_environment = environment.strip().upper()
        latest_explicit: dict[str, object] | None = None
        latest_explicit_sequence = 0
        last_scope_sequence = 0

        events = JournalStore.load_events_by_aggregate_type(
            store,
            "account_reconciliation",
        )
        for checkpoint in events:
            if checkpoint.get("event_type") != "AccountReconciled":
                continue
            payload = checkpoint.get("payload")
            if not isinstance(payload, dict):
                raise RuntimeError("Reconciliation checkpoint payload is invalid")
            if (
                payload.get("provider_id") != normalized_provider
                or payload.get("account_id") != normalized_account
                or payload.get("environment") != normalized_environment
            ):
                continue

            checkpoint_sequence = checkpoint.get("journal_sequence")
            if type(checkpoint_sequence) is not int or checkpoint_sequence <= 0:
                raise RuntimeError(
                    "Reconciliation checkpoint durable ordering identity is invalid"
                )
            if checkpoint_sequence <= last_scope_sequence:
                raise RuntimeError(
                    "Reconciliation journal sequence is not strictly increasing"
                )
            last_scope_sequence = checkpoint_sequence
            if checkpoint_sequence <= ambiguous_sequence:
                continue

            checkpoint_owner = payload.get("checkpoint_owner")
            if not isinstance(checkpoint_owner, dict):
                raise RuntimeError(
                    "Reconciliation checkpoint owner identity is invalid"
                )
            if (
                checkpoint_owner.get("host_id") != self.owner.owner_id
                or checkpoint_owner.get("owner_epoch") != str(self.owner.epoch)
            ):
                continue

            resolutions = payload.get("submission_resolutions")
            if not isinstance(resolutions, list):
                raise RuntimeError(
                    "Reconciliation checkpoint submission_resolutions is invalid"
                )

            explicit = False
            for resolution in resolutions:
                if not isinstance(resolution, dict):
                    raise RuntimeError(
                        "Reconciliation submission resolution is invalid"
                    )
                raw_attempt = resolution.get("attempt_id")
                if not isinstance(raw_attempt, str) or not raw_attempt.strip():
                    raise RuntimeError(
                        "Reconciliation submission resolution attempt identity is invalid"
                    )
                if raw_attempt.strip() == attempt_id:
                    explicit = True
            if not explicit:
                continue

            latest_explicit = checkpoint
            latest_explicit_sequence = checkpoint_sequence

        if latest_explicit is None:
            return False
        if latest_explicit_sequence <= ambiguous_sequence:
            return False

        event_id = latest_explicit.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise RuntimeError("Reconciliation checkpoint event identity is invalid")
        try:
            evidence = load_submission_resolution_evidence(
                store,
                checkpoint_event_id=event_id,
                provider_id=normalized_provider,
                account_id=normalized_account,
                environment=normalized_environment,
                attempt_id=attempt_id,
                intent_id=intent_id,
                client_order_id=client_order_id,
            )
        except (KeyError, TypeError, ValueError):
            # The selected latest explicit verdict is authoritative for this
            # attempt.  If its durable identity cannot be validated exactly, the
            # possible-send barrier remains sticky rather than falling back to
            # an older terminal verdict.
            return False

        return evidence.get("outcome") in {
            "PROVEN_ABSENT",
            "OBSERVED_EXECUTION",
            "OBSERVED_WORKING_ORDER",
        }

    def recover_durable_submission_uncertainty(
        self,
        *,
        environment: str,
        account_id: str,
    ) -> tuple[str, ...]:
        """Atomically rebuild scoped sticky possible-send state from the journal.

        The full selected scope is validated and staged before any controller
        state changes. A malformed later aggregate therefore cannot leave a
        partially projected financial/recovery authority in process memory.
        """

        if self._owner_store is None:
            raise PermissionError(
                "Durable submission recovery requires a journal-backed controller"
            )
        normalized_environment, normalized_account = self._normalized_submission_scope(
            environment,
            account_id,
        )
        if self._owner_scope != f"{normalized_environment}:{normalized_account}":
            raise PermissionError(
                "Submission recovery scope does not match durable recovery owner scope"
            )

        store = self._journal_store_authority()
        events = JournalStore.load_events_by_aggregate_type(
            store,
            "submission_attempt",
        )
        grouped: dict[str, list[dict[str, object]]] = {}
        for event in events:
            aggregate_id = event.get("aggregate_id")
            if not isinstance(aggregate_id, str) or not aggregate_id:
                raise RuntimeError("Submission journal aggregate identity is invalid")
            grouped.setdefault(aggregate_id, []).append(event)

        scope_keys: set[str] = set()
        staged_send_attempts: set[str] = set()
        staged_bindings: dict[str, tuple[str, int, tuple[str, ...]]] = {}
        staged_identities: dict[str, tuple[str, str, str, str, str]] = {}
        staged_legacy = False

        for aggregate_id, aggregate_events in grouped.items():
            first = aggregate_events[0]
            if first.get("event_type") != "SubmissionPrepared":
                raise RuntimeError(
                    "Submission journal aggregate does not start with SubmissionPrepared"
                )
            payload = first.get("payload")
            if not isinstance(payload, dict):
                raise RuntimeError("SubmissionPrepared payload is invalid")
            durable_environment = payload.get("environment")
            durable_account = payload.get("account_id")
            if (
                not isinstance(durable_environment, str)
                or not isinstance(durable_account, str)
            ):
                raise RuntimeError("SubmissionPrepared durable scope is invalid")
            if (
                durable_environment.strip().upper() != normalized_environment
                or durable_account.strip() != normalized_account
            ):
                continue

            sequence = self._validated_submission_event_sequence(
                aggregate_id,
                aggregate_events,
            )
            last = aggregate_events[-1]
            raw_attempt_id = payload.get("attempt_id")
            if isinstance(raw_attempt_id, str) and raw_attempt_id.strip():
                attempt_key = raw_attempt_id.strip()
                expected_aggregate = submission_attempt_aggregate_id(
                    environment=normalized_environment,
                    account_id=normalized_account,
                    attempt_id=attempt_key,
                )
                if expected_aggregate != aggregate_id:
                    raise RuntimeError(
                        "SubmissionPrepared attempt identity does not match durable aggregate"
                    )
            else:
                attempt_key = "legacy_submission:" + aggregate_id
            scope_keys.add(attempt_key)

            if sequence[-1] not in {
                "SubmissionSending",
                "SubmissionUnknown",
            }:
                continue

            if not isinstance(raw_attempt_id, str) or not raw_attempt_id.strip():
                staged_send_attempts.add(attempt_key)
                staged_legacy = True
                continue

            attempt_id = raw_attempt_id.strip()
            intent_id = payload.get("intent_id")
            client_order_id = payload.get("client_order_id")
            provider = payload.get("provider")
            if any(
                not isinstance(value, str) or not value.strip()
                for value in (intent_id, client_order_id, provider)
            ):
                raise RuntimeError(
                    "Ambiguous submission lacks durable reconciliation identity"
                )
            owner_epoch_raw = payload.get("owner_epoch")
            if type(owner_epoch_raw) is not int or owner_epoch_raw <= 0:
                raise RuntimeError("Ambiguous submission owner epoch is invalid")
            event_id = last.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                raise RuntimeError("Ambiguous submission evidence identity is invalid")

            if self._durable_terminal_resolution_dominates_submission(
                store=store,
                ambiguous_event=last,
                attempt_id=attempt_id,
                intent_id=str(intent_id).strip(),
                client_order_id=str(client_order_id).strip(),
                provider_id=str(provider).strip().upper(),
                environment=normalized_environment,
                account_id=normalized_account,
            ):
                continue

            staged_send_attempts.add(attempt_id)
            staged_bindings[attempt_id] = (
                str(intent_id).strip(),
                owner_epoch_raw,
                (event_id,),
            )
            staged_identities[attempt_id] = (
                str(intent_id).strip(),
                str(client_order_id).strip(),
                str(provider).strip().upper(),
                normalized_environment,
                normalized_account,
            )

        prior_scope_keys = set(scope_keys)
        for attempt_id, identity in self._recovered_unknown_identities.items():
            if (
                len(identity) == 5
                and identity[3] == normalized_environment
                and identity[4] == normalized_account
            ):
                prior_scope_keys.add(attempt_id)

        next_send_attempts = set(self._unresolved_send_attempts)
        next_unresolved_attempts = set(self.unresolved_attempts)
        next_bindings = dict(self._unresolved_send_bindings)
        next_identities = dict(self._recovered_unknown_identities)
        for attempt_id in prior_scope_keys:
            next_send_attempts.discard(attempt_id)
            next_unresolved_attempts.discard(attempt_id)
            next_bindings.pop(attempt_id, None)
            next_identities.pop(attempt_id, None)

        next_send_attempts.update(staged_send_attempts)
        next_unresolved_attempts.update(staged_send_attempts)
        next_bindings.update(staged_bindings)
        next_identities.update(staged_identities)

        next_reason_codes = set(self.reason_codes)
        if staged_legacy:
            next_reason_codes.add("legacy_submission_identity_unrecoverable")
        elif not any(
            item.startswith("legacy_submission:") for item in next_send_attempts
        ):
            next_reason_codes.discard("legacy_submission_identity_unrecoverable")
        if staged_send_attempts:
            next_reason_codes.add("provider_uncertainty")

        self._unresolved_send_attempts = next_send_attempts
        self.unresolved_attempts = next_unresolved_attempts
        self._unresolved_send_bindings = next_bindings
        self._recovered_unknown_identities = next_identities
        self.reason_codes = next_reason_codes
        if staged_send_attempts or self.unresolved_attempts:
            self.provider_reconciled = False
        self._recompute_state()
        return tuple(sorted(staged_send_attempts))

    def record_reconciliation_checkpoint(
        self,
        *,
        reconciliation_id: str,
        provider_id: str,
        account_id: str,
        environment: str,
    ) -> dict[str, object]:
        """Derive durable readiness only from current owner-bound provider truth.

        Callers identify the reconciliation scope.  They do not supply a
        consistency boolean, uncertainty list, or copied result.  The canonical
        reconciliation journal remains the authority and must contain the latest
        scope checkpoint for this exact recovery owner epoch.
        """

        if self.owner is None:
            raise RuntimeError("No active owner")
        if self._owner_store is None:
            raise PermissionError(
                "Journal-issued reconciliation requires a durable owner store"
            )
        self._require_current_durable_owner()
        if not self.storage_writable:
            raise PermissionError(
                "Reconciliation cannot establish readiness without durable journal"
            )

        normalized_environment, normalized_account = self._normalized_submission_scope(
            environment,
            account_id,
        )
        if self._owner_scope != f"{normalized_environment}:{normalized_account}":
            raise PermissionError(
                "Reconciliation scope does not match durable recovery owner scope"
            )

        store = self._journal_store_authority()
        checkpoint = load_reconciliation_checkpoint_for_readiness(
            store,
            reconciliation_id=reconciliation_id,
            provider_id=provider_id,
            account_id=normalized_account,
            environment=normalized_environment,
            host_id=self.owner.owner_id,
            owner_epoch=str(self.owner.epoch),
        )
        if checkpoint is None:
            self.provider_reconciled = False
            self.reason_codes.add("startup_reconciliation_required")
            self.reason_codes.add("provider_uncertainty")
            self._recompute_state()
            raise PermissionError(
                "No current reconciliation checkpoint is bound to this recovery owner"
            )

        payload = checkpoint.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("Reconciliation checkpoint payload is invalid")
        blocking = payload.get("blocking_resources")
        resolutions = payload.get("submission_resolutions")
        if not isinstance(blocking, list) or not isinstance(resolutions, list):
            raise RuntimeError("Reconciliation checkpoint readiness fields are invalid")

        reported_unresolved: set[str] = set()
        terminally_resolved_recovered: set[str] = set()
        recovered_resolution_seen: set[str] = set()
        for resource in blocking:
            if not isinstance(resource, str) or not resource.strip():
                raise RuntimeError("Reconciliation blocking resource is invalid")
            reported_unresolved.add(f"resource:{resource.strip()}")
        for resolution in resolutions:
            if not isinstance(resolution, dict):
                raise RuntimeError("Reconciliation submission resolution is invalid")
            outcome = resolution.get("outcome")
            attempt_id = resolution.get("attempt_id")
            if not isinstance(outcome, str):
                raise RuntimeError("Reconciliation submission outcome is invalid")
            normalized_outcome = outcome.strip().upper()
            if normalized_outcome not in {
                "UNKNOWN",
                "PROVEN_ABSENT",
                "OBSERVED_EXECUTION",
                "OBSERVED_WORKING_ORDER",
            }:
                raise RuntimeError(
                    "Reconciliation submission outcome is unsupported"
                )
            provider_order_ids = resolution.get("provider_order_ids", [])
            provider_execution_ids = resolution.get("provider_execution_ids", [])
            if not isinstance(provider_order_ids, list) or not isinstance(
                provider_execution_ids, list
            ):
                raise RuntimeError(
                    "Reconciliation submission provider identities are invalid"
                )
            if any(
                not isinstance(value, str) or not value.strip()
                for value in [*provider_order_ids, *provider_execution_ids]
            ):
                raise RuntimeError(
                    "Reconciliation submission provider identities are invalid"
                )
            normalized_order_ids = tuple(
                value.strip() for value in provider_order_ids
            )
            normalized_execution_ids = tuple(
                value.strip() for value in provider_execution_ids
            )
            if (
                len(normalized_order_ids) != len(set(normalized_order_ids))
                or len(normalized_execution_ids)
                != len(set(normalized_execution_ids))
            ):
                raise RuntimeError(
                    "Reconciliation submission provider identities must be unique"
                )
            if normalized_outcome in {"UNKNOWN", "PROVEN_ABSENT"} and (
                normalized_order_ids or normalized_execution_ids
            ):
                raise RuntimeError(
                    "Absence/unknown reconciliation cannot carry provider identity"
                )
            if (
                normalized_outcome == "OBSERVED_EXECUTION"
                and not normalized_execution_ids
            ):
                raise RuntimeError(
                    "Observed execution reconciliation lacks execution identity"
                )
            if normalized_outcome == "OBSERVED_WORKING_ORDER" and (
                not normalized_order_ids or normalized_execution_ids
            ):
                raise RuntimeError(
                    "Observed working-order reconciliation identity is invalid"
                )
            if normalized_outcome == "UNKNOWN":
                if not isinstance(attempt_id, str) or not attempt_id.strip():
                    raise RuntimeError(
                        "UNKNOWN reconciliation resolution lacks attempt identity"
                    )
                reported_unresolved.add(attempt_id.strip())
            if isinstance(attempt_id, str):
                normalized_attempt = attempt_id.strip()
                recovered_identity = self._recovered_unknown_identities.get(
                    normalized_attempt
                )
                if recovered_identity is not None:
                    if normalized_attempt in recovered_resolution_seen:
                        raise RuntimeError(
                            "Reconciliation checkpoint duplicates recovered submission resolution"
                        )
                    recovered_resolution_seen.add(normalized_attempt)
                    intent_id, client_order_id, provider, recovered_environment, recovered_account = (
                        recovered_identity
                    )
                    if (
                        resolution.get("intent_id") != intent_id
                        or resolution.get("client_order_id") != client_order_id
                        or payload.get("provider_id", "").strip().upper() != provider
                        or payload.get("environment", "").strip().upper()
                        != recovered_environment
                        or payload.get("account_id", "").strip() != recovered_account
                    ):
                        raise RuntimeError(
                            "Reconciliation resolution identity conflicts with recovered submission"
                        )
                    if normalized_outcome in {
                        "PROVEN_ABSENT",
                        "OBSERVED_EXECUTION",
                        "OBSERVED_WORKING_ORDER",
                    }:
                        terminally_resolved_recovered.add(normalized_attempt)

        # Validate the complete durable proof before mutating recovery state.
        # A malformed checkpoint must fail closed without leaving this controller
        # READY from a partially accepted proof.
        event_id = checkpoint.get("event_id")
        payload_hash = checkpoint.get("payload_hash")
        journal_sequence = checkpoint.get("journal_sequence")
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(payload_hash, str)
            or not payload_hash.startswith("sha256:")
            or len(payload_hash) != 71
            or any(ch not in "0123456789abcdef" for ch in payload_hash[7:])
            or type(journal_sequence) is not int
            or journal_sequence <= 0
        ):
            raise RuntimeError("Reconciliation checkpoint durable identity is invalid")

        next_sticky_unknowns = (
            self._unresolved_send_attempts - terminally_resolved_recovered
        )
        next_unresolved_attempts = next_sticky_unknowns | reported_unresolved
        complete = (
            payload.get("complete") is True
            and payload.get("snapshot_consistent") is True
            and payload.get("activity_coverage_complete") is True
        )
        next_provider_reconciled = bool(
            complete and not next_unresolved_attempts
        )

        for attempt_id in terminally_resolved_recovered:
            self._unresolved_send_attempts.discard(attempt_id)
            self._unresolved_send_bindings.pop(attempt_id, None)
            self._recovered_unknown_identities.pop(attempt_id, None)
        self.unresolved_attempts = next_unresolved_attempts
        self.provider_reconciled = next_provider_reconciled
        if self.provider_reconciled:
            self.reason_codes.discard("startup_reconciliation_required")
            self.reason_codes.discard("clock_requalification_required")
            self.reason_codes.discard("provider_uncertainty")
        else:
            self.reason_codes.add("provider_uncertainty")
        self._recompute_state()

        return {
            "event_id": event_id,
            "payload_hash": payload_hash,
            "journal_sequence": journal_sequence,
            "owner_id": self.owner.owner_id,
            "owner_epoch": self.owner.epoch,
        }

    def record_reconciliation(self, *, consistent: bool, uncertainty: Iterable[str] = ()) -> None:
        if self.owner is None:
            raise RuntimeError("No active owner")
        self._require_current_durable_owner()
        if self._owner_store is not None:
            raise PermissionError(
                "Durable recovery requires a journal-issued reconciliation checkpoint"
            )
        if type(consistent) is not bool:
            raise TypeError("consistent must be a boolean")
        if not self.storage_writable:
            raise PermissionError(
                "Reconciliation cannot establish readiness without durable journal"
            )
        reported_unresolved: set[str] = set()
        for item in uncertainty:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    "reconciliation uncertainty identities must be non-empty strings"
                )
            reported_unresolved.add(item.strip())
        # Generic reconciliation uncertainty is snapshot-scoped and may clear
        # on a later clean snapshot.  A previously recorded SENT_UNKNOWN
        # attempt is different: its identity remains sticky until
        # resolve_attempt() proves an evidence-bound terminal phase.
        self.unresolved_attempts = self._unresolved_send_attempts | reported_unresolved
        self.provider_reconciled = bool(consistent and not self.unresolved_attempts)
        if self.provider_reconciled:
            self.reason_codes.discard("startup_reconciliation_required")
            self.reason_codes.discard("clock_requalification_required")
            self.reason_codes.discard("provider_uncertainty")
        else:
            self.reason_codes.add("provider_uncertainty")
        self._recompute_state()

    def set_storage_writable(self, writable: bool) -> None:
        if not isinstance(writable, bool):
            raise TypeError("writable must be a boolean")
        self.storage_writable = writable
        if writable:
            self.reason_codes.discard("durable_journal_unavailable")
        else:
            self.provider_reconciled = False
            self.reason_codes.add("durable_journal_unavailable")
            self.reason_codes.add("startup_reconciliation_required")
        self._recompute_state()

    def set_clock_trusted(self, trusted: bool) -> None:
        if type(trusted) is not bool:
            raise TypeError("trusted must be a boolean")
        self.clock_trusted = trusted
        if trusted:
            self.reason_codes.discard("clock_untrusted")
        else:
            # A clock incident invalidates time-bound reconciliation/freshness
            # evidence. Restoring clock health alone cannot reuse pre-incident
            # readiness evidence.
            self.provider_reconciled = False
            self.reason_codes.add("clock_untrusted")
            self.reason_codes.add("clock_requalification_required")
            self.reason_codes.add("startup_reconciliation_required")
        self._recompute_state()

    def note_unknown_send(self, attempt: OutboundAttempt) -> None:
        if not isinstance(attempt, OutboundAttempt):
            raise TypeError("attempt must be an OutboundAttempt")
        if attempt.phase is not SendPhase.SENT_UNKNOWN:
            raise ValueError("Only uncertain sent attempts block readiness")
        if len(attempt.evidence) != 1:
            raise ValueError(
                "uncertain send must retain exactly one durable send-start evidence ref"
            )
        binding = (
            attempt.intent_id,
            attempt.owner_epoch,
            tuple(attempt.evidence),
        )
        existing = self._unresolved_send_bindings.get(attempt.attempt_id)
        if existing is not None and existing != binding:
            raise ValueError(
                "attempt_id is already bound to different unresolved send identity"
            )
        self._unresolved_send_bindings[attempt.attempt_id] = binding
        self._unresolved_send_attempts.add(attempt.attempt_id)
        self.unresolved_attempts.add(attempt.attempt_id)
        self.provider_reconciled = False
        self.reason_codes.add("provider_uncertainty")
        self._recompute_state()

    def resolve_attempt(self, attempt: OutboundAttempt) -> None:
        if not isinstance(attempt, OutboundAttempt):
            raise TypeError("attempt must be an OutboundAttempt")
        if self._owner_store is not None:
            raise PermissionError(
                "Durable submission uncertainty requires a journal-issued "
                "reconciliation checkpoint"
            )
        if attempt.phase not in {
            SendPhase.ACKNOWLEDGED,
            SendPhase.REJECTED,
            SendPhase.PROVEN_ABSENT,
        }:
            raise ValueError("Attempt is not externally resolved")
        binding = self._unresolved_send_bindings.get(attempt.attempt_id)
        if binding is None:
            raise ValueError("Attempt was not recorded as an unresolved send")
        intent_id, owner_epoch, send_evidence = binding
        if (attempt.intent_id, attempt.owner_epoch) != (intent_id, owner_epoch):
            raise ValueError(
                "Resolved attempt identity does not match unresolved send"
            )
        if tuple(attempt.evidence[: len(send_evidence)]) != send_evidence:
            raise ValueError(
                "Resolved attempt does not preserve original send evidence"
            )
        if len(attempt.evidence) <= len(send_evidence):
            raise ValueError("External resolution requires new terminal evidence")
        if (
            attempt.phase is SendPhase.ACKNOWLEDGED
            and not attempt.provider_order_id
        ):
            raise ValueError("Acknowledged resolution requires provider order identity")
        self._unresolved_send_bindings.pop(attempt.attempt_id, None)
        self._unresolved_send_attempts.discard(attempt.attempt_id)
        self.unresolved_attempts.discard(attempt.attempt_id)
        if not self.unresolved_attempts:
            self.reason_codes.discard("provider_uncertainty")
        self._recompute_state()

    def takeover_durable_owner(self, new_owner_id: str) -> OwnerFence:
        """Fence a stale same-journal sender and claim the next durable epoch.

        The same cross-process sender gate surrounds every irreversible provider
        send. Acquiring it proves no prior sender is currently inside that send
        section. The next durable epoch is committed while the gate is held, so
        the prior process fails its next canonical sender validation.

        This is deliberately local/same-journal fencing only. It does not prove
        a remote host, provider session, credential rotation, or reconciliation
        is safe. The new owner therefore starts RECOVERING and must establish a
        fresh owner-bound reconciliation checkpoint before READY.
        """

        if type(self) is not RecoveryController:
            raise TypeError("durable takeover issuer must be exact RecoveryController")
        if self._owner_store is None:
            raise PermissionError(
                "Durable takeover requires the canonical journal-backed controller"
            )
        if self.owner is not None:
            raise RuntimeError("Controller already has an active owner")
        if not isinstance(new_owner_id, str) or not new_owner_id.strip():
            raise ValueError("New owner identity is required")
        normalized_owner = new_owner_id.strip()

        with journal_sender_gate(self._owner_store):
            durable = self._latest_durable_owner()
            if durable is None:
                raise PermissionError(
                    "No durable owner exists; initial ownership must use start()"
                )
            if normalized_owner == durable.owner_id:
                raise ValueError("Takeover owner must differ from durable owner")
            candidate = OwnerFence(
                owner_id=normalized_owner,
                epoch=durable.epoch + 1,
            )
            self._append_durable_owner(candidate)
            self.owner = candidate
            self.provider_reconciled = False
            self.reason_codes = {"startup_reconciliation_required"}
            self.state = HostState.RECOVERING

        try:
            self._recover_scoped_submission_uncertainty_from_owner_scope()
        except Exception:
            # The epoch already fenced the stale sender. Never roll it back
            # because local projection recovery failed; block until repaired.
            self.provider_reconciled = False
            self.reason_codes.add("takeover_recovery_failed")
            self.reason_codes.add("startup_reconciliation_required")
            self.state = HostState.BLOCKED
            raise
        return candidate

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
        if type(old_sender_fenced) is not bool:
            raise TypeError("old_sender_fenced must be a boolean")
        if type(reconciled) is not bool:
            raise TypeError("reconciled must be a boolean")
        if self._owner_store is not None:
            raise PermissionError(
                "Durable ownership transfer requires independently issued takeover evidence"
            )
        if normalized_owner == self.owner.owner_id:
            raise ValueError("New owner must differ from current owner")
        if not old_sender_fenced:
            raise PermissionError("Old sender must be externally fenced")
        if (
            not reconciled
            or not self.provider_reconciled
            or self.unresolved_attempts
        ):
            raise PermissionError(
                "Ownership transfer requires recorded current reconciliation"
            )
        self._require_current_durable_owner()
        candidate = OwnerFence(normalized_owner, self.owner.epoch + 1)
        self._append_durable_owner(candidate)
        self.owner = candidate
        self.provider_reconciled = False
        self.reason_codes.discard("lease_expired_no_failover")
        self.reason_codes.add("startup_reconciliation_required")
        self.state = HostState.RECOVERING
        return self.owner

    def build_guarded_dispatcher(
        self,
        store: JournalStore,
        *,
        environment: str,
        account_id: str,
        prepared_lease_seconds: int = 60,
    ) -> GuardedDispatcher:
        """Bind dispatch to this controller's exact selected journal generation."""

        if type(self) is not RecoveryController:
            raise TypeError("dispatcher issuer must be exact RecoveryController")
        if self.owner is None:
            raise RuntimeError("No active recovery owner")
        RecoveryController._require_current_durable_owner(self)
        RecoveryController._journal_store_authority(self)
        expected = RecoveryController._selected_journal_identity(self)
        identity = require_exact_journal_store_authority(store)
        if identity != expected:
            raise PermissionError(
                "Dispatcher journal does not match selected recovery authority"
            )
        normalized_environment, normalized_account = (
            RecoveryController._normalized_submission_scope(
                environment,
                account_id,
            )
        )
        if self._owner_scope != f"{normalized_environment}:{normalized_account}":
            raise PermissionError(
                "Dispatcher scope does not match recovery owner scope"
            )
        return _issue_recovery_guarded_dispatcher(
            self,
            store,
            environment=normalized_environment,
            account_id=normalized_account,
            prepared_lease_seconds=prepared_lease_seconds,
        )

    def validate_sender(self, owner_id: str, owner_epoch: int) -> None:
        if not isinstance(owner_epoch, int) or isinstance(owner_epoch, bool) or owner_epoch < 1:
            raise ValueError("owner_epoch must be a positive integer")
        if self.owner is None:
            raise PermissionError("No active sender")
        RecoveryController._require_current_durable_owner(self)
        RecoveryController._recover_scoped_submission_uncertainty_from_owner_scope(self)
        if owner_id != self.owner.owner_id or owner_epoch != self.owner.epoch:
            raise PermissionError("Sender fence mismatch")
        if self.state is not HostState.READY:
            raise PermissionError("Host is not ready for new sends")

    def validate_admission(self, owner_epoch: int) -> None:
        if not isinstance(owner_epoch, int) or isinstance(owner_epoch, bool) or owner_epoch < 1:
            raise ValueError("owner_epoch must be a positive integer")
        if self.owner is not None:
            RecoveryController._require_current_durable_owner(self)
            RecoveryController._recover_scoped_submission_uncertainty_from_owner_scope(self)
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
        self.unresolved_attempts.clear()
        self._unresolved_send_attempts.clear()
        self._unresolved_send_bindings.clear()
        self._recovered_unknown_identities.clear()

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
