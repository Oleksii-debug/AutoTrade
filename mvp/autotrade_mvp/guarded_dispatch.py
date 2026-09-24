"""Fail-closed guarded dispatch foundation for AutoTrade.

No exactly-once claim is made about an external provider. A process failure
after the durable send barrier leaves an UNKNOWN attempt that must reconcile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Callable

from .authority import AuthorityService
from .capabilities import CapabilityError, CapabilityRegistry


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def stable_client_order_id(
    intent_hash: str,
    *,
    provider: str,
    account_id: str,
    max_length: int = 36,
) -> str:
    digest = sha256(
        json.dumps(
            {
                "intent_hash": _text(intent_hash, name="intent_hash"),
                "provider": _text(provider, name="provider").lower(),
                "account_id": _text(account_id, name="account_id"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 16:
        raise ValueError("max_length must be an integer of at least 16")
    return ("at-" + digest)[:max_length]


@dataclass(frozen=True)
class SubmissionAttempt:
    attempt_id: str
    admission_id: str
    intent_hash: str
    provider: str
    account_id: str
    client_order_id: str
    state: str
    detail: str | None = None


class DispatchConflict(ValueError):
    """Raised when immutable dispatch identity or state conflicts."""


class DispatchJournal:
    STATES = frozenset({"PENDING", "SENDING", "SENT", "UNKNOWN", "REJECTED"})

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS submission_attempts (
                attempt_id TEXT PRIMARY KEY,
                admission_id TEXT NOT NULL,
                intent_hash TEXT NOT NULL,
                provider TEXT NOT NULL,
                account_id TEXT NOT NULL,
                client_order_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('PENDING','SENDING','SENT','UNKNOWN','REJECTED')
                ),
                detail TEXT NULL,
                UNIQUE(provider, account_id, client_order_id)
            )
            """
        )

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _from_row(row) -> SubmissionAttempt | None:
        return SubmissionAttempt(*row) if row else None

    def get(self, attempt_id: str) -> SubmissionAttempt | None:
        aid = _text(attempt_id, name="attempt_id")
        row = self._connection.execute(
            """
            SELECT attempt_id, admission_id, intent_hash, provider, account_id,
                   client_order_id, state, detail
            FROM submission_attempts WHERE attempt_id = ?
            """,
            (aid,),
        ).fetchone()
        return self._from_row(row)

    def _get_by_client(
        self,
        *,
        provider: str,
        account_id: str,
        client_order_id: str,
    ) -> SubmissionAttempt | None:
        row = self._connection.execute(
            """
            SELECT attempt_id, admission_id, intent_hash, provider, account_id,
                   client_order_id, state, detail
            FROM submission_attempts
            WHERE provider = ? AND account_id = ? AND client_order_id = ?
            """,
            (provider, account_id, client_order_id),
        ).fetchone()
        return self._from_row(row)

    @staticmethod
    def _same_economic_identity(
        existing: SubmissionAttempt,
        *,
        admission_id: str,
        intent_hash: str,
        provider: str,
        account_id: str,
        client_order_id: str,
    ) -> bool:
        return (
            existing.admission_id,
            existing.intent_hash,
            existing.provider,
            existing.account_id,
            existing.client_order_id,
        ) == (
            admission_id,
            intent_hash,
            provider,
            account_id,
            client_order_id,
        )

    def create(
        self,
        *,
        attempt_id: str,
        admission_id: str,
        intent_hash: str,
        provider: str,
        account_id: str,
        client_order_id: str,
    ) -> SubmissionAttempt:
        aid = _text(attempt_id, name="attempt_id")
        admission = _text(admission_id, name="admission_id")
        ihash = _text(intent_hash, name="intent_hash")
        provider_id = _text(provider, name="provider")
        account = _text(account_id, name="account_id")
        client = _text(client_order_id, name="client_order_id")

        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.get(aid)
            if existing is not None:
                if not self._same_economic_identity(
                    existing,
                    admission_id=admission,
                    intent_hash=ihash,
                    provider=provider_id,
                    account_id=account,
                    client_order_id=client,
                ):
                    raise DispatchConflict(
                        "attempt_id already has different immutable content"
                    )
                connection.commit()
                return existing

            duplicate = self._get_by_client(
                provider=provider_id,
                account_id=account,
                client_order_id=client,
            )
            if duplicate is not None:
                if not self._same_economic_identity(
                    duplicate,
                    admission_id=admission,
                    intent_hash=ihash,
                    provider=provider_id,
                    account_id=account,
                    client_order_id=client,
                ):
                    raise DispatchConflict(
                        "client_order_id already belongs to different immutable content"
                    )
                connection.commit()
                return duplicate

            connection.execute(
                """
                INSERT INTO submission_attempts (
                    attempt_id, admission_id, intent_hash, provider, account_id,
                    client_order_id, state, detail
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', NULL)
                """,
                (aid, admission, ihash, provider_id, account, client),
            )
            created = self.get(aid)
            assert created is not None
            connection.commit()
            return created
        except Exception:
            connection.rollback()
            raise

    def transition(
        self,
        attempt_id: str,
        *,
        expected: set[str],
        state: str,
        detail: str | None = None,
    ) -> SubmissionAttempt:
        aid = _text(attempt_id, name="attempt_id")
        if not expected or not expected <= self.STATES:
            raise ValueError("expected contains unsupported states")
        target = _text(state, name="state").upper()
        if target not in self.STATES:
            raise ValueError("state is unsupported")

        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.get(aid)
            if current is None:
                raise KeyError(aid)
            if current.state not in expected:
                raise DispatchConflict(
                    f"cannot transition {current.state} to {target}"
                )
            connection.execute(
                """
                UPDATE submission_attempts
                SET state = ?, detail = ?
                WHERE attempt_id = ?
                """,
                (target, detail, current.attempt_id),
            )
            updated = self.get(current.attempt_id)
            assert updated is not None
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise

    def recover_interrupted(self) -> int:
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(
                """
                UPDATE submission_attempts
                SET state = 'UNKNOWN',
                    detail = 'process_interrupted_after_send_barrier'
                WHERE state = 'SENDING'
                """
            )
            connection.commit()
            return cursor.rowcount
        except Exception:
            connection.rollback()
            raise


class GuardedDispatcher:
    def __init__(
        self,
        authority: AuthorityService,
        capabilities: CapabilityRegistry,
        journal: DispatchJournal,
    ):
        self.authority = authority
        self.capabilities = capabilities
        self.journal = journal

    def _barriers(
        self,
        *,
        admission_id: str,
        intent_hash: str,
        provider: str,
        account_id: str,
        entity_id: str,
        environment: str,
        instrument: str,
        action: str,
        order_type: str,
        time_in_force: str,
        permission_scope: str,
        now: str,
    ) -> tuple[bool, str]:
        allowed, reason = self.authority.dispatch_allowed(
            admission_id,
            intent_hash=intent_hash,
            account_id=account_id,
            environment=environment,
            instrument=instrument,
            action=action,
            now=now,
        )
        if not allowed:
            return False, reason

        point = _instant(now, name="now")
        try:
            snapshot = self.capabilities.require_verified(
                provider_id=provider,
                account_id=account_id,
                entity_id=entity_id,
                environment=environment,
                instrument_version=instrument,
                at=point,
            )
        except CapabilityError as error:
            return False, f"capability_not_verified:{type(error).__name__}"

        if not snapshot.admits(
            at=point,
            order_type=order_type,
            time_in_force=time_in_force,
            permission_scope=permission_scope,
        ):
            return False, "capability_action_not_admitted"
        return True, "allowed"

    def dispatch(
        self,
        *,
        attempt_id: str,
        admission_id: str,
        intent_hash: str,
        provider: str,
        account_id: str,
        entity_id: str,
        environment: str,
        instrument: str,
        action: str,
        order_type: str,
        time_in_force: str,
        permission_scope: str,
        now: str,
        send: Callable[[str], object],
        wait_before_send: Callable[[], None] | None = None,
    ) -> SubmissionAttempt:
        provider_id = _text(provider, name="provider")
        account = _text(account_id, name="account_id")
        client_order_id = stable_client_order_id(
            intent_hash,
            provider=provider_id,
            account_id=account,
        )
        attempt = self.journal.create(
            attempt_id=attempt_id,
            admission_id=admission_id,
            intent_hash=intent_hash,
            provider=provider_id,
            account_id=account,
            client_order_id=client_order_id,
        )

        if attempt.state in {"SENT", "UNKNOWN", "REJECTED"}:
            return attempt
        if attempt.state == "SENDING":
            return self.journal.transition(
                attempt.attempt_id,
                expected={"SENDING"},
                state="UNKNOWN",
                detail="previous_send_outcome_ambiguous",
            )

        barrier_args = dict(
            admission_id=admission_id,
            intent_hash=intent_hash,
            provider=provider_id,
            account_id=account,
            entity_id=entity_id,
            environment=environment,
            instrument=instrument,
            action=action,
            order_type=order_type,
            time_in_force=time_in_force,
            permission_scope=permission_scope,
            now=now,
        )
        allowed, reason = self._barriers(**barrier_args)
        if not allowed:
            return self.journal.transition(
                attempt.attempt_id,
                expected={"PENDING"},
                state="REJECTED",
                detail=reason,
            )

        if wait_before_send is not None:
            wait_before_send()

        allowed, reason = self._barriers(**barrier_args)
        if not allowed:
            return self.journal.transition(
                attempt.attempt_id,
                expected={"PENDING"},
                state="REJECTED",
                detail=reason,
            )

        self.journal.transition(
            attempt.attempt_id,
            expected={"PENDING"},
            state="SENDING",
            detail="send_barrier_crossed",
        )
        try:
            send(client_order_id)
        except Exception as error:
            return self.journal.transition(
                attempt.attempt_id,
                expected={"SENDING"},
                state="UNKNOWN",
                detail=f"provider_send_ambiguous:{type(error).__name__}",
            )
        return self.journal.transition(
            attempt.attempt_id,
            expected={"SENDING"},
            state="SENT",
            detail="provider_call_returned",
        )
