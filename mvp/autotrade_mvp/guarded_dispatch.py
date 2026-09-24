"""Fail-closed guarded dispatch foundation for AutoTrade.

The module deliberately does not claim exactly-once external execution.
A crash or exception after the send barrier leaves an UNKNOWN attempt that
must reconcile before any retry is considered.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Callable

from .authority import AuthorityService


def stable_client_order_id(intent_hash: str, *, provider: str, account_id: str, max_length: int = 36) -> str:
    if not isinstance(intent_hash, str) or not intent_hash.strip():
        raise ValueError("intent_hash is required")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider is required")
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id is required")
    if not isinstance(max_length, int) or max_length < 16:
        raise ValueError("max_length must be at least 16")
    digest = sha256(
        json.dumps(
            {
                "intent_hash": intent_hash.strip(),
                "provider": provider.strip().lower(),
                "account_id": account_id.strip(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
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
    """Raised when immutable dispatch identity is reused inconsistently."""


class DispatchJournal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS submission_attempts (
                attempt_id TEXT PRIMARY KEY,
                admission_id TEXT NOT NULL,
                intent_hash TEXT NOT NULL,
                provider TEXT NOT NULL,
                account_id TEXT NOT NULL,
                client_order_id TEXT NOT NULL,
                state TEXT NOT NULL,
                detail TEXT NULL
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def get(self, attempt_id: str) -> SubmissionAttempt | None:
        row = self._connection.execute(
            """
            SELECT attempt_id, admission_id, intent_hash, provider, account_id,
                   client_order_id, state, detail
            FROM submission_attempts WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        return SubmissionAttempt(*row) if row else None

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
        existing = self.get(attempt_id)
        candidate = SubmissionAttempt(
            attempt_id=attempt_id,
            admission_id=admission_id,
            intent_hash=intent_hash,
            provider=provider,
            account_id=account_id,
            client_order_id=client_order_id,
            state="PENDING",
        )
        if existing is not None:
            if (
                existing.admission_id,
                existing.intent_hash,
                existing.provider,
                existing.account_id,
                existing.client_order_id,
            ) != (
                candidate.admission_id,
                candidate.intent_hash,
                candidate.provider,
                candidate.account_id,
                candidate.client_order_id,
            ):
                raise DispatchConflict("attempt_id already has different immutable content")
            return existing
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO submission_attempts (
                    attempt_id, admission_id, intent_hash, provider, account_id,
                    client_order_id, state, detail
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', NULL)
                """,
                (attempt_id, admission_id, intent_hash, provider, account_id, client_order_id),
            )
        return candidate

    def transition(self, attempt_id: str, *, expected: set[str], state: str, detail: str | None = None) -> SubmissionAttempt:
        current = self.get(attempt_id)
        if current is None:
            raise KeyError(attempt_id)
        if current.state not in expected:
            raise DispatchConflict(f"cannot transition {current.state} to {state}")
        with self._connection:
            self._connection.execute(
                "UPDATE submission_attempts SET state = ?, detail = ? WHERE attempt_id = ?",
                (state, detail, attempt_id),
            )
        updated = self.get(attempt_id)
        assert updated is not None
        return updated

    def recover_interrupted(self) -> int:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE submission_attempts
                SET state = 'UNKNOWN', detail = 'process_interrupted_after_send_barrier'
                WHERE state = 'SENDING'
                """
            )
        return cursor.rowcount


class GuardedDispatcher:
    def __init__(self, authority: AuthorityService, journal: DispatchJournal):
        self.authority = authority
        self.journal = journal

    def dispatch(
        self,
        *,
        attempt_id: str,
        admission_id: str,
        intent_hash: str,
        provider: str,
        account_id: str,
        now: str,
        send: Callable[[str], object],
        wait_before_send: Callable[[], None] | None = None,
    ) -> SubmissionAttempt:
        client_order_id = stable_client_order_id(
            intent_hash, provider=provider, account_id=account_id
        )
        attempt = self.journal.create(
            attempt_id=attempt_id,
            admission_id=admission_id,
            intent_hash=intent_hash,
            provider=provider,
            account_id=account_id,
            client_order_id=client_order_id,
        )

        if attempt.state in {"SENT", "UNKNOWN"}:
            return attempt
        if attempt.state == "REJECTED":
            return attempt
        if attempt.state == "SENDING":
            return self.journal.transition(
                attempt_id,
                expected={"SENDING"},
                state="UNKNOWN",
                detail="previous_send_outcome_ambiguous",
            )

        allowed, reason = self.authority.dispatch_allowed(
            admission_id, intent_hash=intent_hash, now=now
        )
        if not allowed:
            return self.journal.transition(
                attempt_id,
                expected={"PENDING"},
                state="REJECTED",
                detail=reason,
            )

        if wait_before_send is not None:
            wait_before_send()

        allowed, reason = self.authority.dispatch_allowed(
            admission_id, intent_hash=intent_hash, now=now
        )
        if not allowed:
            return self.journal.transition(
                attempt_id,
                expected={"PENDING"},
                state="REJECTED",
                detail=reason,
            )

        self.journal.transition(
            attempt_id,
            expected={"PENDING"},
            state="SENDING",
            detail="send_barrier_crossed",
        )
        try:
            send(client_order_id)
        except Exception as error:
            return self.journal.transition(
                attempt_id,
                expected={"SENDING"},
                state="UNKNOWN",
                detail=f"provider_send_ambiguous:{type(error).__name__}",
            )
        return self.journal.transition(
            attempt_id,
            expected={"SENDING"},
            state="SENT",
            detail="provider_call_returned",
        )
