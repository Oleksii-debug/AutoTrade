"""Durable guarded submission attempts for the AutoTrade MVP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5, uuid4

from .persistence import JournalStore, canonical_json, payload_digest


AuthorityCheck = Callable[[str, str], tuple[bool, str]]
TransportSend = Callable[[str, Mapping[str, Any], Callable[[], None]], Any]


class DispatchBlocked(RuntimeError):
    """Raised inside a provider wrapper when the final send barrier rejects."""


@dataclass(frozen=True)
class DispatchOutcome:
    status: str
    client_order_id: str
    response: Any | None
    reason: str


def _identity_digest(*parts: str) -> str:
    """Hash a canonical tuple without delimiter-boundary ambiguity."""
    return sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()


def stable_client_order_id(
    provider: str,
    intent_id: str,
    *,
    environment: str,
    account_id: str,
    max_length: int = 32,
) -> str:
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider is required")
    if not isinstance(intent_id, str) or not intent_id.strip():
        raise ValueError("intent_id is required")
    normalized_environment = environment.strip().upper() if isinstance(environment, str) else ""
    if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id is required")
    if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length < 20:
        raise ValueError(
            "max_length must be an integer of at least 20 "
            "to preserve client-order identity entropy"
        )
    digest = _identity_digest(
        provider.strip().lower(),
        normalized_environment,
        account_id.strip(),
        intent_id.strip(),
    )
    return ("at-" + digest)[:max_length]


def _instant(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("now must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("now must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("now must include a timezone")
    return parsed.astimezone(timezone.utc)


def _event_id(scope_key: str, attempt_id: str, event_type: str, version: int) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            "dispatch-event:" + _identity_digest(
                scope_key,
                attempt_id,
                event_type,
                str(version),
            ),
        )
    )


def _envelope(
    *,
    scope_key: str,
    aggregate_id: str,
    environment: str,
    attempt_id: str,
    event_type: str,
    version: int,
    payload: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    timestamp = _instant(now).isoformat().replace("+00:00", "Z")
    return {
        "event_id": _event_id(scope_key, attempt_id, event_type, version),
        "event_type": event_type,
        "schema_version": "1.0.0",
        "aggregate_type": "submission_attempt",
        "aggregate_id": aggregate_id,
        "aggregate_version": str(version),
        "host_id": "local-mvp",
        "owner_epoch": "1",
        "environment": environment,
        "occurred_at": timestamp,
        "observed_at": timestamp,
        "committed_at": timestamp,
        "correlation_id": str(
            uuid5(
                NAMESPACE_URL,
                "dispatch-correlation:" + _identity_digest(scope_key, attempt_id),
            )
        ),
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }


class GuardedDispatcher:
    """Persist-before-send dispatcher that never blindly retries ambiguity.

    Provider wrappers receive a final_guard callback and must invoke it
    immediately before the actual outbound request. Provider qualification must
    prove that contract with an outbound-request counter.
    """

    def __init__(
        self,
        store: JournalStore,
        *,
        environment: str,
        account_id: str,
        owner_token: str | None = None,
        prepared_lease_seconds: int = 60,
    ):
        self.store = store
        normalized_environment = (
            environment.strip().upper() if isinstance(environment, str) else ""
        )
        if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("account_id is required")
        self.environment = normalized_environment
        self.account_id = account_id.strip()
        self.scope_key = _identity_digest(self.environment, self.account_id)
        self.owner_token = owner_token or str(uuid4())
        if not isinstance(prepared_lease_seconds, int) or isinstance(prepared_lease_seconds, bool) or prepared_lease_seconds < 1:
            raise ValueError("prepared_lease_seconds must be a positive integer")
        self.prepared_lease_seconds = prepared_lease_seconds

    def _aggregate_id(self, attempt_id: str) -> str:
        return "submission-attempt:" + _identity_digest(
            self.environment,
            self.account_id,
            attempt_id,
        )

    def _events(self, attempt_id: str) -> list[dict[str, Any]]:
        return self.store.load_events(
            "submission_attempt",
            self._aggregate_id(attempt_id),
        )

    def _append(
        self,
        *,
        attempt_id: str,
        event_type: str,
        version: int,
        payload: dict[str, Any],
        now: str,
    ):
        return self.store.append_event(
            _envelope(
                scope_key=self.scope_key,
                aggregate_id=self._aggregate_id(attempt_id),
                environment=self.environment,
                attempt_id=attempt_id,
                event_type=event_type,
                version=version,
                payload=payload,
                now=now,
            ),
            outbox_topic="autotrade.submission.events",
        )

    @staticmethod
    def _outcome_from_terminal(event: dict[str, Any], client_order_id: str) -> DispatchOutcome:
        payload = event["payload"]
        if event["event_type"] == "SubmissionSent":
            return DispatchOutcome("SENT", client_order_id, payload.get("response"), "sent_confirmed")
        if event["event_type"] == "SubmissionBlocked":
            return DispatchOutcome("BLOCKED", client_order_id, None, payload.get("reason", "blocked"))
        if event["event_type"] == "SubmissionUnknown":
            return DispatchOutcome("UNKNOWN", client_order_id, None, payload.get("reason", "unknown"))
        raise ValueError("event is not terminal")

    def _recover_existing(
        self,
        *,
        attempt_id: str,
        client_order_id: str,
        now: str,
    ) -> DispatchOutcome:
        events = self._events(attempt_id)
        if not events:
            raise RuntimeError("submission attempt disappeared")
        last = events[-1]
        if last["event_type"] in {"SubmissionSent", "SubmissionBlocked", "SubmissionUnknown"}:
            return self._outcome_from_terminal(last, client_order_id)
        if last["event_type"] == "SubmissionSending":
            self._append(
                attempt_id=attempt_id,
                event_type="SubmissionUnknown",
                version=last["aggregate_version"] + 1,
                payload={
                    "client_order_id": client_order_id,
                    "reason": "recovered_after_send_barrier_without_terminal_result",
                },
                now=now,
            )
            return self._outcome_from_terminal(self._events(attempt_id)[-1], client_order_id)
        if last["event_type"] != "SubmissionPrepared":
            raise RuntimeError(f"unsupported submission attempt state: {last['event_type']}")

        prepared_at = _instant(last["payload"]["prepared_at"])
        age = (_instant(now) - prepared_at).total_seconds()
        if age < 0:
            return DispatchOutcome("IN_PROGRESS", client_order_id, None, "clock_before_prepared_timestamp")
        if age < self.prepared_lease_seconds:
            return DispatchOutcome("IN_PROGRESS", client_order_id, None, "prepared_owner_lease_active")
        self._append(
            attempt_id=attempt_id,
            event_type="SubmissionUnknown",
            version=last["aggregate_version"] + 1,
            payload={
                "client_order_id": client_order_id,
                "reason": "prepared_owner_lease_expired_without_send_evidence",
            },
            now=now,
        )
        return self._outcome_from_terminal(self._events(attempt_id)[-1], client_order_id)

    def dispatch(
        self,
        *,
        attempt_id: str,
        intent_id: str,
        intent_hash: str,
        provider: str,
        request: Mapping[str, Any],
        now: str,
        authority_check: AuthorityCheck,
        transport_send: TransportSend,
        client_id_max_length: int = 32,
        final_barrier_clock: Callable[[], str] | None = None,
    ) -> DispatchOutcome:
        for value, name in (
            (attempt_id, "attempt_id"),
            (intent_id, "intent_id"),
            (intent_hash, "intent_hash"),
            (provider, "provider"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        _instant(now)
        request_dict = dict(request)
        request_hash = "sha256:" + sha256(canonical_json(request_dict).encode("utf-8")).hexdigest()
        client_order_id = stable_client_order_id(
            provider,
            intent_id,
            environment=self.environment,
            account_id=self.account_id,
            max_length=client_id_max_length,
        )

        existing = self._events(attempt_id)
        if existing:
            prepared = existing[0]["payload"]
            expected = {
                "intent_id": intent_id,
                "intent_hash": intent_hash,
                "provider": provider,
                "request_hash": request_hash,
                "client_order_id": client_order_id,
                "environment": self.environment,
                "account_id": self.account_id,
            }
            if any(prepared.get(key) != value for key, value in expected.items()):
                raise ValueError("attempt_id conflicts with existing submission content")
            return self._recover_existing(
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                now=now,
            )

        prepared_payload = {
            "intent_id": intent_id,
            "intent_hash": intent_hash,
            "provider": provider,
            "request_hash": request_hash,
            "client_order_id": client_order_id,
            "environment": self.environment,
            "account_id": self.account_id,
            "owner_token": self.owner_token,
            "prepared_at": _instant(now).isoformat().replace("+00:00", "Z"),
        }
        prepared = self._append(
            attempt_id=attempt_id,
            event_type="SubmissionPrepared",
            version=1,
            payload=prepared_payload,
            now=now,
        )
        if not prepared.inserted:
            return self._recover_existing(
                attempt_id=attempt_id,
                client_order_id=client_order_id,
                now=now,
            )

        allowed, reason = authority_check(intent_hash, now)
        if not allowed:
            self._append(
                attempt_id=attempt_id,
                event_type="SubmissionBlocked",
                version=2,
                payload={"client_order_id": client_order_id, "reason": reason},
                now=now,
            )
            return DispatchOutcome("BLOCKED", client_order_id, None, reason)

        guard_called = False
        barrier_now = now

        def final_guard() -> None:
            nonlocal guard_called, barrier_now
            if guard_called:
                raise RuntimeError("final send guard may be consumed only once")
            guard_called = True
            if final_barrier_clock is not None:
                barrier_now = final_barrier_clock()
                _instant(barrier_now)
                if _instant(barrier_now) < _instant(now):
                    barrier_now = now
                    self._append(
                        attempt_id=attempt_id,
                        event_type="SubmissionBlocked",
                        version=2,
                        payload={
                            "client_order_id": client_order_id,
                            "reason": "final_barrier_clock_moved_backwards",
                        },
                        now=barrier_now,
                    )
                    raise DispatchBlocked("final_barrier_clock_moved_backwards")
            allowed_now, barrier_reason = authority_check(intent_hash, barrier_now)
            if not allowed_now:
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionBlocked",
                    version=2,
                    payload={"client_order_id": client_order_id, "reason": barrier_reason},
                    now=barrier_now,
                )
                raise DispatchBlocked(barrier_reason)
            self._append(
                attempt_id=attempt_id,
                event_type="SubmissionSending",
                version=2,
                payload={
                    "client_order_id": client_order_id,
                    "owner_token": self.owner_token,
                    "reason": "final_send_barrier_passed",
                },
                now=barrier_now,
            )

        try:
            response = transport_send(client_order_id, request_dict, final_guard)
        except DispatchBlocked as error:
            return DispatchOutcome("BLOCKED", client_order_id, None, str(error))
        except Exception as error:
            events = self._events(attempt_id)
            last = events[-1]
            if last["event_type"] == "SubmissionSending":
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionUnknown",
                    version=3,
                    payload={
                        "client_order_id": client_order_id,
                        "reason": f"transport_exception_after_send_barrier:{type(error).__name__}",
                    },
                    now=barrier_now,
                )
                return DispatchOutcome("UNKNOWN", client_order_id, None, "transport_result_ambiguous")
            if not guard_called:
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionBlocked",
                    version=2,
                    payload={
                        "client_order_id": client_order_id,
                        "reason": f"transport_failed_before_final_guard:{type(error).__name__}",
                    },
                    now=now,
                )
                return DispatchOutcome("BLOCKED", client_order_id, None, "transport_failed_before_send")
            raise

        if not guard_called:
            self._append(
                attempt_id=attempt_id,
                event_type="SubmissionUnknown",
                version=2,
                payload={
                    "client_order_id": client_order_id,
                    "reason": "provider_wrapper_returned_without_final_guard",
                },
                now=now,
            )
            return DispatchOutcome("UNKNOWN", client_order_id, None, "provider_guard_contract_violation")

        try:
            self._append(
                attempt_id=attempt_id,
                event_type="SubmissionSent",
                version=3,
                payload={"client_order_id": client_order_id, "response": response},
                now=barrier_now,
            )
        except Exception as persistence_error:
            # The outbound request has already crossed the final barrier.
            # Never make this state safe to retry merely because the provider
            # response could not be journaled.
            try:
                self._append(
                    attempt_id=attempt_id,
                    event_type="SubmissionUnknown",
                    version=3,
                    payload={
                        "client_order_id": client_order_id,
                        "reason": (
                            "sent_response_persistence_failed:"
                            + type(persistence_error).__name__
                        ),
                    },
                    now=barrier_now,
                )
            except Exception:
                # A durable SubmissionSending row already exists. Recovery will
                # convert that state to UNKNOWN without another outbound send.
                raise persistence_error
            return DispatchOutcome(
                "UNKNOWN",
                client_order_id,
                None,
                "sent_response_persistence_failed",
            )
        return DispatchOutcome("SENT", client_order_id, response, "sent_confirmed")
