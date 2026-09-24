from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
from typing import Any


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        raise TypeError("money values must use Decimal")
    if not value.is_finite():
        raise ValueError("money values must be finite")
    return format(value, "f")


@dataclass(frozen=True, slots=True)
class ModelCostRecord:
    request_id: str
    model_id: str
    provider_id: str
    revision: str | None
    reserved_cost: Decimal
    incurred_cost: Decimal
    estimated_unbilled: Decimal
    status: str


class ModelCostStore:
    """Durable cost accounting for already-admitted model calls.

    This store is deliberately downstream of model routing. It cannot select a
    model and cannot expand tool or trading authority. It records exact Decimal
    ceilings and settlement so restart cannot erase model spending.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path, *, ceiling: Decimal):
        if not isinstance(ceiling, Decimal):
            raise TypeError("ceiling must use Decimal")
        if not ceiling.is_finite() or ceiling < 0:
            raise ValueError("ceiling must be a finite non-negative Decimal")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ceiling = ceiling
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS model_cost_schema "
                "(version INTEGER PRIMARY KEY, ceiling_text TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT version, ceiling_text FROM model_cost_schema ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO model_cost_schema(version, ceiling_text) VALUES (?, ?)",
                    (self.SCHEMA_VERSION, _decimal_text(self.ceiling)),
                )
            else:
                if row["version"] > self.SCHEMA_VERSION:
                    connection.rollback()
                    raise ValueError("model-cost schema is newer than this runtime")
                stored = Decimal(row["ceiling_text"])
                if stored != self.ceiling:
                    connection.rollback()
                    raise ValueError("budget ceiling conflicts with persisted store")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_costs (
                    request_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    revision TEXT,
                    reserved_text TEXT NOT NULL,
                    incurred_text TEXT NOT NULL,
                    unbilled_text TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('RESERVED','SETTLED','RELEASED')
                    )
                );

                CREATE TRIGGER IF NOT EXISTS model_cost_identity_immutable
                BEFORE UPDATE OF model_id, provider_id, revision, reserved_text
                ON model_costs
                BEGIN
                    SELECT RAISE(ABORT, 'model cost identity is immutable');
                END;
                """
            )
            connection.commit()

    @staticmethod
    def _require_text(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-empty text")
        return value

    @staticmethod
    def _d(value: str) -> Decimal:
        try:
            parsed = Decimal(value)
        except (InvalidOperation, TypeError) as error:
            raise ValueError("invalid persisted Decimal") from error
        if not parsed.is_finite():
            raise ValueError("invalid persisted Decimal")
        return parsed

    def snapshot(self) -> dict[str, Decimal]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT reserved_text, incurred_text, unbilled_text, status FROM model_costs"
            ).fetchall()
        reserved = Decimal("0")
        incurred = Decimal("0")
        unbilled = Decimal("0")
        for row in rows:
            if row["status"] == "RESERVED":
                reserved += self._d(row["reserved_text"])
            incurred += self._d(row["incurred_text"])
            unbilled += self._d(row["unbilled_text"])
        used = reserved + incurred + unbilled
        available = self.ceiling - used
        if available < 0:
            raise ValueError("persisted model costs exceed configured ceiling")
        return {
            "ceiling": self.ceiling,
            "reserved": reserved,
            "incurred": incurred,
            "estimated_unbilled": unbilled,
            "available": available,
        }

    def reserve(
        self,
        *,
        request_id: str,
        model_id: str,
        provider_id: str,
        revision: str | None,
        amount: Decimal,
    ) -> bool:
        self._require_text(request_id, "request_id")
        self._require_text(model_id, "model_id")
        self._require_text(provider_id, "provider_id")
        amount_text = _decimal_text(amount)
        if amount < 0:
            raise ValueError("reservation cannot be negative")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM model_costs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                exact = (
                    existing["model_id"] == model_id
                    and existing["provider_id"] == provider_id
                    and existing["revision"] == revision
                    and existing["reserved_text"] == amount_text
                )
                connection.commit()
                if not exact:
                    raise ValueError("request_id conflicts with persisted model identity")
                return False

            rows = connection.execute(
                "SELECT reserved_text, incurred_text, unbilled_text, status FROM model_costs"
            ).fetchall()
            used = Decimal("0")
            for row in rows:
                if row["status"] == "RESERVED":
                    used += self._d(row["reserved_text"])
                used += self._d(row["incurred_text"]) + self._d(row["unbilled_text"])
            if amount > self.ceiling - used:
                connection.rollback()
                raise ValueError("budget exhausted")

            connection.execute(
                """
                INSERT INTO model_costs(
                    request_id, model_id, provider_id, revision,
                    reserved_text, incurred_text, unbilled_text, status
                ) VALUES (?, ?, ?, ?, ?, '0', '0', 'RESERVED')
                """,
                (request_id, model_id, provider_id, revision, amount_text),
            )
            connection.commit()
        return True

    def release(self, request_id: str) -> bool:
        self._require_text(request_id, "request_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM model_costs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(request_id)
            if row["status"] == "RELEASED":
                connection.commit()
                return False
            if row["status"] != "RESERVED":
                connection.rollback()
                raise ValueError("settled model cost cannot be released")
            connection.execute(
                "UPDATE model_costs SET status = 'RELEASED' WHERE request_id = ?",
                (request_id,),
            )
            connection.commit()
        return True

    def settle(
        self,
        request_id: str,
        *,
        incurred: Decimal,
        estimated_unbilled: Decimal = Decimal("0"),
    ) -> bool:
        self._require_text(request_id, "request_id")
        incurred_text = _decimal_text(incurred)
        unbilled_text = _decimal_text(estimated_unbilled)
        if incurred < 0 or estimated_unbilled < 0:
            raise ValueError("costs cannot be negative")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM model_costs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(request_id)
            if row["status"] == "SETTLED":
                exact = (
                    row["incurred_text"] == incurred_text
                    and row["unbilled_text"] == unbilled_text
                )
                connection.commit()
                if not exact:
                    raise ValueError("settlement conflicts with persisted result")
                return False
            if row["status"] != "RESERVED":
                connection.rollback()
                raise ValueError("released reservation cannot settle")
            reserved = self._d(row["reserved_text"])
            if incurred + estimated_unbilled > reserved:
                connection.rollback()
                raise ValueError("settlement exceeds reserved ceiling")
            connection.execute(
                """
                UPDATE model_costs
                SET incurred_text = ?, unbilled_text = ?, status = 'SETTLED'
                WHERE request_id = ?
                """,
                (incurred_text, unbilled_text, request_id),
            )
            connection.commit()
        return True

    def reconcile_unbilled(self, request_id: str, *, billed: Decimal) -> None:
        self._require_text(request_id, "request_id")
        billed_text = _decimal_text(billed)
        if billed < 0:
            raise ValueError("billed cost cannot be negative")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, incurred_text, unbilled_text FROM model_costs WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(request_id)
            if row["status"] != "SETTLED":
                connection.rollback()
                raise ValueError("only settled cost can reconcile billing")
            unbilled = self._d(row["unbilled_text"])
            incurred = self._d(row["incurred_text"])
            if billed > unbilled:
                connection.rollback()
                raise ValueError("billed cost exceeds estimated unbilled amount")
            connection.execute(
                "UPDATE model_costs SET incurred_text = ?, unbilled_text = ? WHERE request_id = ?",
                (_decimal_text(incurred + billed), _decimal_text(unbilled - billed), request_id),
            )
            connection.commit()

    def get(self, request_id: str) -> ModelCostRecord:
        self._require_text(request_id, "request_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_costs WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            raise KeyError(request_id)
        return ModelCostRecord(
            request_id=row["request_id"],
            model_id=row["model_id"],
            provider_id=row["provider_id"],
            revision=row["revision"],
            reserved_cost=self._d(row["reserved_text"]),
            incurred_cost=self._d(row["incurred_text"]),
            estimated_unbilled=self._d(row["unbilled_text"]),
            status=row["status"],
        )
