"""Durable journal-backed projection for reservation authority.

This module reuses the canonical JournalStore; it does not create a second
persistence database.  Every reservation mutation is first committed as one
versioned journal event, then the in-memory ReservationBook is rebuilt from
that durable history.  A crash between journal commit and process projection is
therefore recovered by replay, while a stale concurrent writer fails the
JournalStore aggregate-version check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping
from uuid import UUID, NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)
from research.autotrade_research.io.strict_json import strict_json_loads

from .dispatch import submission_attempt_aggregate_id
from .persistence import JournalStore, canonical_json, payload_digest
from .reservations import (
    ReservationBook,
    ReservationConflict,
    ReservationSnapshot,
)


_AGGREGATE_TYPE = "reservation_book"
_EVENT_TYPE = "ReservationMutationCommitted"
_COMMAND_ACTOR = "autotrade-reservation-authority"
_RESOLUTION_MEDIA_TYPE = "application/vnd.autotrade.reservation-resolution+json"
_RESOLUTION_EVIDENCE_TYPE = "AUTOTRADE_RESERVATION_RESOLUTION"
_RESOLUTION_SCHEMA_VERSION = 2


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()



def _immutable_evidence_ref(value: str) -> tuple[str, str, str]:
    reference = _text(value, name="resolution_evidence")
    marker = "@sha256:"
    if not reference.startswith("artifact:") or marker not in reference:
        raise ValueError(
            "resolution_evidence must bind an immutable artifact and SHA-256 digest"
        )
    artifact_id, digest = reference[len("artifact:"):].split(marker, 1)
    try:
        artifact_id = str(UUID(artifact_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(
            "resolution_evidence artifact identity must be a UUID"
        ) from error
    if len(digest) != 64 or any(
        ch not in "0123456789abcdef" for ch in digest
    ):
        raise ValueError(
            "resolution_evidence must use canonical lowercase SHA-256"
        )
    return artifact_id, digest, f"artifact:{artifact_id}@sha256:{digest}"

def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _amount_map(values: Mapping[str, object], *, allow_zero: bool) -> dict[str, str]:
    if not isinstance(values, Mapping) or not values:
        raise ValueError("resource amounts are required")
    result: dict[str, str] = {}
    for resource, raw in values.items():
        key = _text(resource, name="resource")
        if key in result:
            raise ValueError("resource names must be unique after normalization")
        amount = _decimal(raw, name=f"amount[{key}]")
        if amount < 0 or (amount == 0 and not allow_zero):
            raise ValueError("resource amounts must be positive")
        result[key] = _decimal_text(amount)
    return dict(sorted(result.items()))


def _snapshot_payload(snapshot: ReservationSnapshot) -> dict[str, object]:
    return {
        "reservation_id": snapshot.reservation_id,
        "intent_id": snapshot.intent_id,
        "original": {
            key: _decimal_text(value)
            for key, value in sorted(snapshot.original.items())
        },
        "remaining": {
            key: _decimal_text(value)
            for key, value in sorted(snapshot.remaining.items())
        },
        "consumed": {
            key: _decimal_text(value)
            for key, value in sorted(snapshot.consumed.items())
        },
        "state": snapshot.state,
        "resolution_evidence": snapshot.resolution_evidence,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _environment(value: str) -> str:
    normalized = value.strip().upper() if isinstance(value, str) else ""
    if normalized not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    return normalized


def _journal_identity(
    environment: str,
    account_id: str,
    kind: str,
    external_id: str,
) -> str:
    """Scope generic JournalStore identities to one account/environment tuple."""

    environment = _environment(environment)
    account = _text(account_id, name="account_id")
    identity_kind = _text(kind, name="identity_kind")
    external = _text(external_id, name="external_id")
    canonical = canonical_json(
        [environment, account, identity_kind, external]
    )
    return str(uuid5(NAMESPACE_URL, "reservation-identity:" + canonical))


@dataclass(frozen=True)
class PreparedReservationMutation:
    """One reservation mutation prepared from a single durable journal cut."""

    snapshot: ReservationSnapshot
    snapshot_payload: dict[str, object]
    envelope: dict[str, object] | None
    idempotency_key: str
    request: dict[str, object]
    aggregate_version: int
    already_committed: bool = False


class DurableReservationBook:
    """ReservationBook projection with crash/restart and dedupe semantics."""

    def __init__(
        self,
        store: JournalStore,
        *,
        environment: str,
        account_id: str,
        resolution_artifact_store: ArtifactStore | None = None,
    ):
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        self.store = store
        self.environment = _environment(environment)
        self.account_id = _text(account_id, name="account_id")
        if (
            resolution_artifact_store is not None
            and not isinstance(resolution_artifact_store, ArtifactStore)
        ):
            raise TypeError("resolution_artifact_store must be ArtifactStore or None")
        self.resolution_artifact_store = resolution_artifact_store
        self.scope_id = _journal_identity(
            self.environment,
            self.account_id,
            "scope",
            "reservation-book",
        )
        self._book = ReservationBook()
        self._idempotency: dict[str, tuple[str, dict[str, object]]] = {}
        self._reload()

    def _events(self) -> list[dict[str, object]]:
        return self.store.load_events(_AGGREGATE_TYPE, self.scope_id)

    def _replay(
        self,
        events: list[dict[str, object]],
    ) -> tuple[
        ReservationBook,
        dict[str, tuple[str, dict[str, object]]],
    ]:
        book = ReservationBook()
        idempotency: dict[str, tuple[str, dict[str, object]]] = {}
        expected_version = 1

        for event in events:
            if event["aggregate_version"] != expected_version:
                raise ReservationConflict(
                    "reservation journal aggregate versions are not contiguous"
                )
            expected_version += 1
            if event["event_type"] != _EVENT_TYPE:
                raise ReservationConflict(
                    "reservation journal contains an unsupported event type"
                )
            if payload_digest(event["payload"]) != event["payload_hash"]:
                raise ReservationConflict(
                    "reservation journal payload hash does not match stored payload"
                )
            payload = event["payload"]
            if not isinstance(payload, dict):
                raise ReservationConflict("reservation event payload must be an object")
            operation = payload.get("operation")
            request = payload.get("request")
            expected_snapshot = payload.get("snapshot")
            idem = payload.get("idempotency_key")
            request_hash = payload.get("request_hash")
            if not isinstance(request, dict):
                raise ReservationConflict("reservation event request must be an object")
            if not isinstance(expected_snapshot, dict):
                raise ReservationConflict("reservation event snapshot must be an object")
            idem = _text(idem, name="idempotency_key")
            request_hash = _text(request_hash, name="request_hash")
            if request_hash != payload_digest(request):
                raise ReservationConflict(
                    "reservation event request hash does not match request"
                )
            prior = idempotency.get(idem)
            if prior is not None:
                if prior[0] != request_hash:
                    raise ReservationConflict(
                        "reservation idempotency key has conflicting journal requests"
                    )
                raise ReservationConflict(
                    "duplicate reservation idempotency event must not be appended"
                )

            try:
                if operation == "MARK_TERMINAL":
                    current = book.get(request.get("reservation_id"))
                    self._verify_resolution_evidence(
                        reservation_id=request.get("reservation_id"),
                        intent_id=current.intent_id,
                        outcome=request.get("outcome"),
                        provider=request.get("provider"),
                        attempt_id=request.get("attempt_id"),
                        resolution_evidence=request.get("resolution_evidence"),
                    )
                snapshot = self._apply(book, operation, request)
            except Exception as error:
                raise ReservationConflict(
                    f"reservation journal cannot be replayed at version "
                    f"{event['aggregate_version']}: {error}"
                ) from error
            actual = _snapshot_payload(snapshot)
            if actual != expected_snapshot:
                raise ReservationConflict(
                    "reservation journal snapshot does not match replayed state"
                )
            idempotency[idem] = (request_hash, actual)

        return book, idempotency

    @staticmethod
    def _apply(
        book: ReservationBook,
        operation: object,
        request: dict[str, object],
    ) -> ReservationSnapshot:
        if operation == "RESERVE":
            return book.reserve(
                reservation_id=request["reservation_id"],
                intent_id=request["intent_id"],
                requirements=request["requirements"],
                available=request["available"],
            )
        if operation == "CONSUME":
            return book.consume(
                request["reservation_id"],
                request["usage"],
            )
        if operation == "MARK_UNKNOWN":
            return book.mark_unknown(request["reservation_id"])
        if operation == "MARK_TERMINAL":
            return book.mark_terminal(
                request["reservation_id"],
                outcome=request["outcome"],
                resolution_evidence=request["resolution_evidence"],
            )
        raise ReservationConflict(f"unsupported reservation operation: {operation}")

    def _reload(self) -> None:
        self._book, self._idempotency = self._replay(self._events())

    def _existing(
        self,
        *,
        idempotency_key: str,
        request: dict[str, object],
    ) -> dict[str, object] | None:
        key = _text(idempotency_key, name="idempotency_key")
        existing = self._idempotency.get(key)
        if existing is None:
            return None
        current_hash = payload_digest(request)
        if existing[0] != current_hash:
            raise ReservationConflict(
                "idempotency_key was already used for a different reservation request"
            )
        return existing[1]

    def prepare_reserve_mutation(
        self,
        *,
        event_key: str,
        idempotency_key: str,
        reservation_id: str,
        intent_id: str,
        requirements: Mapping[str, object],
        available: Mapping[str, object],
        committed_at: str,
    ) -> PreparedReservationMutation:
        """Prepare, but do not commit, a worst-case reservation.

        This is used by the financial admission writer so reservation, risk
        evidence, confirmation consumption, admission and outbox publication
        can share one JournalStore.commit_command transaction. The plan is
        derived from exactly one reservation journal cut; a concurrent writer
        therefore fails the aggregate-version fence at commit rather than
        reusing stale availability.
        """

        key = _text(idempotency_key, name="idempotency_key")
        request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
            "intent_id": _text(intent_id, name="intent_id"),
            "requirements": _amount_map(requirements, allow_zero=False),
            "available": _amount_map(available, allow_zero=True),
        }
        events = self._events()
        candidate, idempotency = self._replay(events)
        existing = idempotency.get(key)
        if existing is not None:
            if existing[0] != payload_digest(request):
                raise ReservationConflict(
                    "idempotency_key was already used for a different reservation request"
                )
            raise ReservationConflict(
                "reservation mutation is already committed; replay the financial command"
            )

        snapshot = self._apply(candidate, "RESERVE", request)
        snapshot_value = _snapshot_payload(snapshot)
        next_version = (
            1 if not events else int(events[-1]["aggregate_version"]) + 1
        )
        payload = {
            "environment": self.environment,
            "account_id": self.account_id,
            "operation": "RESERVE",
            "request": request,
            "idempotency_key": key,
            "request_hash": payload_digest(request),
            "snapshot": snapshot_value,
        }
        envelope = {
            "event_id": _journal_identity(
                self.environment,
                self.account_id,
                "financial-admission-reservation-event",
                _text(event_key, name="event_key"),
            ),
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.scope_id,
            "aggregate_version": str(next_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _text(committed_at, name="committed_at"),
        }
        return PreparedReservationMutation(
            snapshot=snapshot,
            snapshot_payload=snapshot_value,
            envelope=envelope,
            idempotency_key=key,
            request=request,
            aggregate_version=next_version,
        )

    def prepare_consume_mutation(
        self,
        *,
        event_key: str,
        idempotency_key: str,
        reservation_id: str,
        usage: Mapping[str, object],
        committed_at: str,
        expected_snapshot_digest: str | None = None,
        evidence_binding: Mapping[str, object] | None = None,
    ) -> PreparedReservationMutation:
        """Prepare one reservation consumption for a shared durable commit.

        No reservation state is mutated here. The returned event is derived
        from one immutable reservation-journal cut and can be committed in the
        same JournalStore transaction as canonical economic events. Exact
        replay after acknowledgement loss reports already_committed only when
        the same idempotency key, request and resulting snapshot are already
        present in durable reservation history. When
        expected_snapshot_digest is supplied, the plan is fenced to the exact
        reservation cut from which provider-fill resource usage was derived.
        evidence_binding is persisted inside the immutable reservation request
        but never changes ReservationBook resource semantics.
        """

        key = _text(idempotency_key, name="idempotency_key")
        request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
            "usage": _amount_map(usage, allow_zero=False),
        }
        if evidence_binding is not None:
            if not isinstance(evidence_binding, Mapping):
                raise TypeError("evidence_binding must be a mapping or None")
            try:
                normalized_binding = strict_json_loads(
                    canonical_json(dict(evidence_binding))
                )
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "evidence_binding must be canonical JSON-compatible evidence"
                ) from error
            if type(normalized_binding) is not dict or not normalized_binding:
                raise ValueError(
                    "evidence_binding must be a non-empty JSON object"
                )
            request["evidence_binding"] = normalized_binding
        expected_cut = (
            None
            if expected_snapshot_digest is None
            else _text(
                expected_snapshot_digest,
                name="expected_snapshot_digest",
            )
        )
        events = self._events()
        candidate, idempotency = self._replay(events)
        existing = idempotency.get(key)
        if existing is not None:
            if existing[0] != payload_digest(request):
                raise ReservationConflict(
                    "idempotency_key was already used for a different reservation request"
                )
            snapshot = candidate.get(request["reservation_id"])
            snapshot_value = _snapshot_payload(snapshot)
            if snapshot_value != existing[1]:
                raise ReservationConflict(
                    "committed reservation consumption snapshot does not match replayed state"
                )
            return PreparedReservationMutation(
                snapshot=snapshot,
                snapshot_payload=snapshot_value,
                envelope=None,
                idempotency_key=key,
                request=request,
                aggregate_version=(
                    0
                    if not events
                    else int(events[-1]["aggregate_version"])
                ),
                already_committed=True,
            )

        if expected_cut is not None:
            current_snapshot = candidate.get(request["reservation_id"])
            current_cut = payload_digest(_snapshot_payload(current_snapshot))
            if current_cut != expected_cut:
                raise ReservationConflict(
                    "reservation snapshot changed after provider fill plan derivation"
                )

        snapshot = self._apply(candidate, "CONSUME", request)
        snapshot_value = _snapshot_payload(snapshot)
        next_version = (
            1 if not events else int(events[-1]["aggregate_version"]) + 1
        )
        payload = {
            "environment": self.environment,
            "account_id": self.account_id,
            "operation": "CONSUME",
            "request": request,
            "idempotency_key": key,
            "request_hash": payload_digest(request),
            "snapshot": snapshot_value,
        }
        envelope = {
            "event_id": _journal_identity(
                self.environment,
                self.account_id,
                "financial-fill-reservation-event",
                _text(event_key, name="event_key"),
            ),
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.scope_id,
            "aggregate_version": str(next_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _text(committed_at, name="committed_at"),
        }
        return PreparedReservationMutation(
            snapshot=snapshot,
            snapshot_payload=snapshot_value,
            envelope=envelope,
            idempotency_key=key,
            request=request,
            aggregate_version=next_version,
        )

    def refresh(self) -> None:
        """Reload the reservation projection after an external atomic commit."""

        self._reload()

    def _commit(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        operation: str,
        request: dict[str, object],
    ) -> ReservationSnapshot:
        cid = _text(command_id, name="command_id")
        idem = _text(idempotency_key, name="idempotency_key")

        # Read one journal snapshot for idempotency, financial availability
        # and aggregate version. A second read here would allow a concurrent
        # identical command to appear between dedupe and candidate replay,
        # causing the same economic mutation to be applied twice locally before
        # commit_command can return its durable idempotent result.
        events = self._events()
        candidate, idempotency = self._replay(events)
        existing = idempotency.get(idem)
        if existing is not None:
            current_hash = payload_digest(request)
            if existing[0] != current_hash:
                raise ReservationConflict(
                    "idempotency_key was already used for a different reservation request"
                )
            self._book = candidate
            self._idempotency = idempotency
            return self.get(existing[1]["reservation_id"])

        # The candidate and aggregate version come from that same journal cut.
        try:
            snapshot = self._apply(candidate, operation, request)
        except Exception:
            # Keep the exposed projection synchronized even when evaluation
            # rejects the mutation.
            self._reload()
            raise
        snapshot_value = _snapshot_payload(snapshot)
        next_version = (
            1
            if not events
            else int(events[-1]["aggregate_version"]) + 1
        )
        event_id = _journal_identity(
            self.environment,
            self.account_id,
            "event",
            canonical_json([cid, str(next_version)]),
        )
        journal_command_id = _journal_identity(
            self.environment,
            self.account_id,
            "command",
            cid,
        )
        journal_idempotency_key = _journal_identity(
            self.environment,
            self.account_id,
            "idempotency",
            idem,
        )
        payload = {
            "environment": self.environment,
            "account_id": self.account_id,
            "operation": operation,
            "request": request,
            "idempotency_key": idem,
            "request_hash": payload_digest(request),
            "snapshot": snapshot_value,
        }
        envelope = {
            "event_id": event_id,
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.scope_id,
            "aggregate_version": str(next_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _now(),
        }

        # commit_command is the single durable transaction: command dedupe and
        # reservation event either both commit or neither does.
        try:
            self.store.commit_command(
                command_id=journal_command_id,
                actor=_COMMAND_ACTOR,
                environment=self.environment,
                idempotency_key=journal_idempotency_key,
                request={
                    "environment": self.environment,
                    "account_id": self.account_id,
                    "operation": operation,
                    "request": request,
                },
                result=snapshot_value,
                state_version=next_version,
                events=[(envelope, None)],
            )
        except Exception:
            # A competing writer may have committed after this projection was
            # built. Never leave this authority object serving stale financial
            # availability after the optimistic-concurrency fence rejects us.
            self._reload()
            raise
        self._reload()
        return self.get(snapshot.reservation_id)

    def get(self, reservation_id: str) -> ReservationSnapshot:
        self._reload()
        return self._book.get(reservation_id)

    def total_reserved(self, resource: str) -> Decimal:
        self._reload()
        return self._book.total_reserved(resource)

    def active(self) -> tuple[ReservationSnapshot, ...]:
        self._reload()
        return self._book.active()

    @property
    def version(self) -> int:
        events = self._events()
        return 0 if not events else int(events[-1]["aggregate_version"])

    @property
    def state_digest(self) -> str:
        """Content identity for the exact durable reservation journal cut.

        The digest is derived from the canonical account/environment scope and
        the ordered immutable event identities/hashes. It is evidence of this
        projection cut only; it does not create a second reservation authority.
        """

        events = self._events()
        version = 0 if not events else int(events[-1]["aggregate_version"])
        material = {
            "environment": self.environment,
            "account_id": self.account_id,
            "version": version,
            "events": [
                {
                    "event_id": event["event_id"],
                    "aggregate_version": str(event["aggregate_version"]),
                    "payload_hash": event["payload_hash"],
                }
                for event in events
            ],
        }
        return payload_digest(material).removeprefix("sha256:")

    def reserve(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        reservation_id: str,
        intent_id: str,
        requirements: Mapping[str, object],
        available: Mapping[str, object],
    ) -> ReservationSnapshot:
        request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
            "intent_id": _text(intent_id, name="intent_id"),
            "requirements": _amount_map(requirements, allow_zero=False),
            "available": _amount_map(available, allow_zero=True),
        }
        return self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="RESERVE",
            request=request,
        )

    def consume(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        reservation_id: str,
        usage: Mapping[str, object],
    ) -> ReservationSnapshot:
        request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
            "usage": _amount_map(usage, allow_zero=False),
        }
        return self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="CONSUME",
            request=request,
        )

    def mark_unknown(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        reservation_id: str,
    ) -> ReservationSnapshot:
        request = {
            "reservation_id": _text(reservation_id, name="reservation_id"),
        }
        return self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="MARK_UNKNOWN",
            request=request,
        )

    def _verify_resolution_evidence(
        self,
        *,
        reservation_id: object,
        intent_id: object,
        outcome: object,
        provider: object,
        attempt_id: object,
        resolution_evidence: object,
    ) -> str:
        rid = _text(reservation_id, name="reservation_id")
        intent = _text(intent_id, name="intent_id")
        terminal_outcome = _text(outcome, name="outcome").upper()
        provider_name = _text(provider, name="provider").upper()
        attempt = _text(attempt_id, name="attempt_id")
        artifact_id, digest, evidence = _immutable_evidence_ref(
            resolution_evidence
        )
        if self.resolution_artifact_store is None:
            raise ReservationConflict(
                "terminal release requires the trusted resolution artifact store"
            )
        try:
            manifest = self.resolution_artifact_store.load_manifest(artifact_id)
            manifest_hash = manifest.get("manifest_hash")
            if (
                not isinstance(manifest_hash, str)
                or not manifest_hash.startswith("sha256:")
                or len(manifest_hash) != 71
            ):
                raise ArtifactIntegrityError(
                    "resolution evidence manifest lacks canonical integrity binding"
                )
            if manifest.get("sha256") != f"sha256:{digest}":
                raise ArtifactIntegrityError(
                    "resolution evidence reference digest does not match manifest"
                )
            if manifest.get("media_type") != _RESOLUTION_MEDIA_TYPE:
                raise ArtifactIntegrityError(
                    "resolution evidence has an unsupported media type"
                )
            raw = self.resolution_artifact_store.read_bytes(artifact_id)
            text = raw.decode("utf-8")
            receipt = strict_json_loads(text)
        except (
            ArtifactIntegrityError,
            FileNotFoundError,
            UnicodeError,
            ValueError,
            TypeError,
        ) as error:
            raise ReservationConflict(
                "resolution evidence verification failed"
            ) from error
        if type(receipt) is not dict:
            raise ReservationConflict(
                "resolution evidence receipt must be a JSON object"
            )

        try:
            reconciliation_event_id = _text(
                receipt.get("reconciliation_event_id"),
                name="reconciliation_event_id",
            )
            reconciliation_payload_hash = _text(
                receipt.get("reconciliation_payload_hash"),
                name="reconciliation_payload_hash",
            )
        except (ValueError, TypeError) as error:
            raise ReservationConflict(
                "resolution evidence receipt does not match reservation scope"
            ) from error
        if (
            not reconciliation_payload_hash.startswith("sha256:")
            or len(reconciliation_payload_hash) != 71
            or any(
                ch not in "0123456789abcdef"
                for ch in reconciliation_payload_hash.removeprefix("sha256:")
            )
        ):
            raise ReservationConflict(
                "resolution evidence receipt does not match reservation scope"
            )

        expected = {
            "schema_version": _RESOLUTION_SCHEMA_VERSION,
            "evidence_type": _RESOLUTION_EVIDENCE_TYPE,
            "environment": self.environment,
            "account_id": self.account_id,
            "reservation_id": rid,
            "intent_id": intent,
            "provider": provider_name,
            "attempt_id": attempt,
            "outcome": terminal_outcome,
            "reconciliation_complete": True,
            "reconciliation_event_id": reconciliation_event_id,
            "reconciliation_payload_hash": reconciliation_payload_hash,
        }
        receipt_canonical = canonical_json(receipt)
        expected_canonical = canonical_json(expected)
        if receipt_canonical != expected_canonical:
            raise ReservationConflict(
                "resolution evidence receipt does not match reservation scope"
            )
        if raw != receipt_canonical.encode("utf-8"):
            raise ReservationConflict(
                "resolution evidence receipt must use canonical JSON bytes"
            )

        aggregate_id = submission_attempt_aggregate_id(
            environment=self.environment,
            account_id=self.account_id,
            attempt_id=attempt,
        )
        attempt_events = self.store.load_events(
            "submission_attempt",
            aggregate_id,
        )
        if not attempt_events:
            raise ReservationConflict(
                "resolution evidence is not bound to a durable submission attempt"
            )
        prepared = attempt_events[0]
        if prepared.get("event_type") != "SubmissionPrepared":
            raise ReservationConflict(
                "submission attempt does not start with durable preparation"
            )
        prepared_payload = prepared.get("payload")
        if not isinstance(prepared_payload, dict):
            raise ReservationConflict(
                "submission attempt preparation payload is invalid"
            )
        if (
            prepared_payload.get("environment") != self.environment
            or prepared_payload.get("account_id") != self.account_id
            or _text(
                prepared_payload.get("provider"),
                name="submission provider",
            ).upper()
            != provider_name
            or prepared_payload.get("intent_id") != intent
        ):
            raise ReservationConflict(
                "resolution evidence does not match durable submission scope"
            )
        client_order_id = _text(
            prepared_payload.get("client_order_id"),
            name="submission client_order_id",
        )
        if (
            terminal_outcome == "PROVEN_ABSENT"
            and not any(
                event.get("event_type") == "SubmissionUnknown"
                for event in attempt_events
            )
        ):
            raise ReservationConflict(
                "PROVEN_ABSENT requires a durable UNKNOWN submission state"
            )

        reconciliation_event = self.store.get_event(reconciliation_event_id)
        if (
            reconciliation_event is None
            or reconciliation_event.get("event_type") != "AccountReconciled"
            or reconciliation_event.get("aggregate_type") != "account_reconciliation"
        ):
            raise ReservationConflict(
                "terminal release requires a matching durable reconciliation checkpoint"
            )
        reconciliation_payload = reconciliation_event.get("payload")
        if not isinstance(reconciliation_payload, dict):
            raise ReservationConflict(
                "durable reconciliation checkpoint payload is invalid"
            )
        if (
            reconciliation_event.get("payload_hash")
            != reconciliation_payload_hash
            or payload_digest(reconciliation_payload)
            != reconciliation_payload_hash
        ):
            raise ReservationConflict(
                "durable reconciliation checkpoint hash does not match receipt"
            )
        if (
            reconciliation_payload.get("provider_id") != provider_name
            or reconciliation_payload.get("account_id") != self.account_id
            or reconciliation_payload.get("environment") != self.environment
        ):
            raise ReservationConflict(
                "durable reconciliation checkpoint scope does not match reservation"
            )
        if (
            reconciliation_payload.get("complete") is not True
            or reconciliation_payload.get("snapshot_consistent") is not True
            or reconciliation_payload.get("blocking_resources") != []
        ):
            raise ReservationConflict(
                "terminal release requires complete non-blocking reconciliation"
            )
        resolutions = reconciliation_payload.get("submission_resolutions")
        if not isinstance(resolutions, list):
            raise ReservationConflict(
                "durable reconciliation checkpoint lacks submission resolutions"
            )
        matching = [
            item
            for item in resolutions
            if isinstance(item, dict)
            and item.get("attempt_id") == attempt
            and item.get("client_order_id") == client_order_id
        ]
        if len(matching) != 1:
            raise ReservationConflict(
                "durable reconciliation checkpoint does not uniquely resolve submission"
            )
        canonical_outcome = _text(
            matching[0].get("outcome"),
            name="reconciliation submission outcome",
        ).upper()
        # Reconciliation can prove that at least one execution exists, but an
        # execution observation alone does not prove that the order is fully
        # filled.  Keep worst-case reservation capacity held until a canonical
        # terminal order/fill projection can prove FILLED semantics.
        required_outcome = {
            "PROVEN_ABSENT": "PROVEN_ABSENT",
        }.get(terminal_outcome)
        if required_outcome is None:
            raise ReservationConflict(
                "terminal outcome lacks canonical reconciliation semantics"
            )
        if canonical_outcome != required_outcome:
            raise ReservationConflict(
                "terminal outcome does not match durable reconciliation resolution"
            )
        return evidence

    def mark_terminal(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        reservation_id: str,
        outcome: str,
        provider: str,
        attempt_id: str,
        resolution_evidence: str,
    ) -> ReservationSnapshot:
        rid = _text(reservation_id, name="reservation_id")
        current = self.get(rid)
        terminal_outcome = _text(outcome, name="outcome").upper()
        provider_name = _text(provider, name="provider").upper()
        attempt = _text(attempt_id, name="attempt_id")
        evidence = self._verify_resolution_evidence(
            reservation_id=rid,
            intent_id=current.intent_id,
            outcome=terminal_outcome,
            provider=provider_name,
            attempt_id=attempt,
            resolution_evidence=resolution_evidence,
        )
        request = {
            "reservation_id": rid,
            "outcome": terminal_outcome,
            "provider": provider_name,
            "attempt_id": attempt,
            "resolution_evidence": evidence,
        }
        return self._commit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            operation="MARK_TERMINAL",
            request=request,
        )
