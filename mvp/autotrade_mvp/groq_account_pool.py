"""Durable, secret-free Groq quota admission and account selection.

The pool is deliberately separate from credentials and HTTP transport.  It
persists only non-secret profile aliases, model identifiers, quota windows and
usage counters.  Every configured account retains an entirely independent
profile-and-model quota ledger; capacity is never transferred or netted between
profiles.

Reservation happens before the outbound model boundary.  A reservation that
does not reach a verified response is retained conservatively and the same
attempt is never sent again through this wrapper.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Mapping

from autotrade_foundation.local_filesystem import (
    LocalFilesystemQualificationError,
    freeze_local_filesystem_path,
    require_qualified_local_filesystem_path,
)

from .model_call import ModelCallBinding, ModelCallObservation


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
_PROFILE = re.compile(r"groq-account-[1-9][0-9]{0,2}\Z")
_PROVIDER = "groq"
_MINUTE_SECONDS = 60
_DAY_SECONDS = 24 * 60 * 60
_RESET_VALUE = re.compile(r"[0-9][0-9.smhd]{0,63}\Z")
_RATE_LIMIT_HEADERS = (
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
)


class GroqQuotaError(RuntimeError):
    """Sanitized quota/admission failure."""


class GroqQuotaExhausted(GroqQuotaError):
    """No compliant account has enough capacity for the requested ceiling."""


class GroqAttemptAlreadyReserved(GroqQuotaError):
    """The attempt crossed the durable pre-send boundary and cannot be resent."""


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} is invalid")
    return value


def _profile(value: object) -> str:
    if type(value) is not str or not _PROFILE.fullmatch(value):
        raise ValueError("Groq profile alias is invalid")
    return value


def _positive_int(value: object, name: str, maximum: int = 10_000_000) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} is outside the allowed range")
    return value


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    normalized = value.astimezone(timezone.utc)
    if normalized.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return normalized


def _window_start(now: datetime, seconds: int) -> int:
    timestamp = int(now.timestamp())
    return timestamp - timestamp % seconds


@dataclass(frozen=True, slots=True)
class GroqModelQuota:
    model_id: str
    requests_per_minute: int
    tokens_per_minute: int
    requests_per_day: int
    tokens_per_day: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", _identifier(self.model_id, "model_id"))
        for field_name in (
            "requests_per_minute",
            "tokens_per_minute",
            "requests_per_day",
            "tokens_per_day",
        ):
            object.__setattr__(
                self,
                field_name,
                _positive_int(getattr(self, field_name), field_name),
            )
        if self.requests_per_minute > self.requests_per_day:
            raise ValueError("minute request quota cannot exceed daily quota")
        if self.tokens_per_minute > self.tokens_per_day:
            raise ValueError("minute token quota cannot exceed daily quota")


# Explicitly dated policy material.  Product composition may replace this tuple
# when a later provider policy is reviewed; reopening an existing journal with
# changed limits fails closed instead of silently changing its accounting.
GROQ_FREE_PLAN_QUOTAS_2026_10_20 = tuple(
    GroqModelQuota(model_id, 30, 8_000, 1_000, 200_000)
    for model_id in (
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
    )
)


@dataclass(frozen=True, slots=True)
class GroqAccountCompliancePolicy:
    """Non-secret account aliases and their explicitly enabled subset."""

    stored_profiles: tuple[str, ...] = (
        "groq-account-1",
        "groq-account-2",
        "groq-account-3",
        "groq-account-4",
    )
    active_profiles: tuple[str, ...] = (
        "groq-account-1",
        "groq-account-2",
        "groq-account-3",
        "groq-account-4",
    )

    def __post_init__(self) -> None:
        if type(self.stored_profiles) is not tuple or type(self.active_profiles) is not tuple:
            raise TypeError("Groq profiles must be tuples")
        stored = tuple(_profile(value) for value in self.stored_profiles)
        active = tuple(_profile(value) for value in self.active_profiles)
        if not stored or len(stored) != len(set(stored)):
            raise ValueError("stored Groq profiles must be nonempty and unique")
        if not active or len(active) != len(set(active)):
            raise ValueError("active Groq profiles must be nonempty and unique")
        if not set(active).issubset(stored):
            raise ValueError("active Groq profiles must be stored profiles")
        object.__setattr__(self, "stored_profiles", tuple(sorted(stored)))
        object.__setattr__(self, "active_profiles", tuple(sorted(active)))


@dataclass(frozen=True, slots=True)
class GroqQuotaReservation:
    reservation_id: str
    attempt_id: str
    profile: str
    model_id: str
    reserved_tokens: int
    minute_window_start: int
    day_window_start: int

    def __post_init__(self) -> None:
        _identifier(self.reservation_id, "reservation_id")
        _identifier(self.attempt_id, "attempt_id")
        _profile(self.profile)
        _identifier(self.model_id, "model_id")
        _positive_int(self.reserved_tokens, "reserved_tokens")
        if type(self.minute_window_start) is not int or self.minute_window_start < 0:
            raise ValueError("minute window is invalid")
        if type(self.day_window_start) is not int or self.day_window_start < 0:
            raise ValueError("day window is invalid")


@dataclass(frozen=True, slots=True)
class GroqQuotaSnapshot:
    profile: str
    model_id: str
    minute_requests_remaining: int
    minute_tokens_remaining: int
    day_requests_remaining: int
    day_tokens_remaining: int
    observed_at: str
    provider_observation: "GroqProviderLimitObservation | None" = None


@dataclass(frozen=True, slots=True)
class GroqProviderLimitObservation:
    """Sanitized provider headers for diagnostics, never admission authority."""

    limit_requests: int | None
    remaining_requests: int | None
    reset_requests: str | None
    limit_tokens: int | None
    remaining_tokens: int | None
    reset_tokens: str | None
    observed_at: str


def sanitize_groq_rate_limit_headers(
    headers: Mapping[str, str], *, observed_at: datetime
) -> GroqProviderLimitObservation | None:
    """Retain only bounded official counters; discard every other header."""

    if not isinstance(headers, Mapping):
        raise TypeError("Groq response headers must be a mapping")
    selected: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        if type(raw_name) is not str or type(raw_value) is not str:
            continue
        name = raw_name.lower()
        if name in _RATE_LIMIT_HEADERS and name not in selected:
            selected[name] = raw_value
    if not selected:
        return None

    def counter(name: str) -> int | None:
        value = selected.get(name)
        if value is None or not value.isascii() or not value.isdecimal():
            return None
        parsed = int(value)
        return parsed if 0 <= parsed <= 1_000_000_000 else None

    def reset(name: str) -> str | None:
        value = selected.get(name)
        if value is None or not value.isascii() or not _RESET_VALUE.fullmatch(value):
            return None
        return value

    observation = GroqProviderLimitObservation(
        counter("x-ratelimit-limit-requests"),
        counter("x-ratelimit-remaining-requests"),
        reset("x-ratelimit-reset-requests"),
        counter("x-ratelimit-limit-tokens"),
        counter("x-ratelimit-remaining-tokens"),
        reset("x-ratelimit-reset-tokens"),
        _utc(observed_at, "observed_at").isoformat().replace("+00:00", "Z"),
    )
    if all(
        value is None
        for value in (
            observation.limit_requests,
            observation.remaining_requests,
            observation.reset_requests,
            observation.limit_tokens,
            observation.remaining_tokens,
            observation.reset_tokens,
        )
    ):
        return None
    return observation


class DurableGroqQuotaJournal:
    """SQLite-backed atomic quota authority with no credential material."""

    _SCHEMA_VERSION = "1"

    def __init__(
        self,
        path: str | Path,
        *,
        policy: GroqAccountCompliancePolicy,
        quotas: tuple[GroqModelQuota, ...],
    ) -> None:
        if type(policy) is not GroqAccountCompliancePolicy:
            raise TypeError("Groq compliance policy is invalid")
        if type(quotas) is not tuple or not quotas:
            raise TypeError("Groq quotas must be a nonempty tuple")
        if any(type(item) is not GroqModelQuota for item in quotas):
            raise TypeError("Groq quota entries must be exact GroqModelQuota values")
        canonical = tuple(sorted(quotas, key=lambda item: item.model_id))
        if len({item.model_id for item in canonical}) != len(canonical):
            raise ValueError("Groq model quotas must be unique")
        if type(path) not in (str, type(Path())):
            raise TypeError("Groq quota path must be exact text or Path")
        frozen = Path(freeze_local_filesystem_path(Path(path).expanduser()))
        try:
            require_qualified_local_filesystem_path(frozen)
        except LocalFilesystemQualificationError as error:
            raise GroqQuotaError(
                "Groq quota database requires a qualified local filesystem"
            ) from error
        frozen.parent.mkdir(parents=True, exist_ok=True)
        self.path = frozen
        self.policy = policy
        self.quotas = canonical
        self._quota_by_model = {item.model_id: item for item in canonical}
        self._configuration = self._configuration_json(policy, canonical)
        self._initialize()

    @staticmethod
    def _configuration_json(
        policy: GroqAccountCompliancePolicy,
        quotas: tuple[GroqModelQuota, ...],
    ) -> str:
        return json.dumps(
            {
                "provider": _PROVIDER,
                "stored_profiles": list(policy.stored_profiles),
                "active_profiles": list(policy.active_profiles),
                "quotas": [
                    {
                        "model_id": item.model_id,
                        "requests_per_minute": item.requests_per_minute,
                        "tokens_per_minute": item.tokens_per_minute,
                        "requests_per_day": item.requests_per_day,
                        "tokens_per_day": item.tokens_per_day,
                    }
                    for item in quotas
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS quota_metadata ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS quota_reservations ("
                    "attempt_id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL UNIQUE, "
                    "profile TEXT NOT NULL, model_id TEXT NOT NULL, "
                    "reserved_tokens INTEGER NOT NULL CHECK(reserved_tokens > 0), "
                    "minute_window_start INTEGER NOT NULL, "
                    "day_window_start INTEGER NOT NULL, "
                    "state TEXT NOT NULL CHECK(state IN ('RESERVED','SETTLED','AMBIGUOUS')), "
                    "actual_tokens INTEGER, created_at TEXT NOT NULL, settled_at TEXT)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS provider_limit_observations ("
                    "attempt_id TEXT PRIMARY KEY, profile TEXT NOT NULL, "
                    "model_id TEXT NOT NULL, observation_json TEXT NOT NULL)"
                )
                metadata = {
                    "schema_version": self._SCHEMA_VERSION,
                    "configuration": self._configuration,
                }
                for key, value in metadata.items():
                    existing = connection.execute(
                        "SELECT value FROM quota_metadata WHERE key = ?", (key,)
                    ).fetchone()
                    if existing is None:
                        connection.execute(
                            "INSERT INTO quota_metadata(key, value) VALUES (?, ?)",
                            (key, value),
                        )
                    elif existing["value"] != value:
                        raise GroqQuotaError(
                            "Groq quota configuration conflicts with durable state"
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _usage_expression() -> str:
        return "CASE WHEN state = 'SETTLED' THEN actual_tokens ELSE reserved_tokens END"

    def _used(
        self,
        connection: sqlite3.Connection,
        *,
        profile: str,
        model_id: str,
        window_column: str,
        window_start: int,
    ) -> tuple[int, int]:
        if window_column not in ("minute_window_start", "day_window_start"):
            raise ValueError("quota window is invalid")
        row = connection.execute(
            f"SELECT COUNT(*) AS requests, "
            f"COALESCE(SUM({self._usage_expression()}), 0) AS tokens "
            f"FROM quota_reservations WHERE profile = ? AND model_id = ? "
            f"AND {window_column} = ?",
            (profile, model_id, window_start),
        ).fetchone()
        if row is None or type(row["requests"]) is not int or type(row["tokens"]) is not int:
            raise GroqQuotaError("Groq quota projection is invalid")
        return row["requests"], row["tokens"]

    def reserve(
        self,
        *,
        attempt_id: str,
        model_id: str,
        maximum_input_tokens: int,
        maximum_output_tokens: int,
        now: datetime,
        required_profile: str | None = None,
    ) -> GroqQuotaReservation:
        attempt = _identifier(attempt_id, "attempt_id")
        model = _identifier(model_id, "model_id")
        input_ceiling = _positive_int(maximum_input_tokens, "maximum_input_tokens")
        output_ceiling = _positive_int(maximum_output_tokens, "maximum_output_tokens")
        token_ceiling = input_ceiling + output_ceiling
        if token_ceiling > 10_000_000:
            raise ValueError("combined token ceiling is outside the allowed range")
        instant = _utc(now, "now")
        quota = self._quota_by_model.get(model)
        if quota is None:
            raise GroqQuotaError("Groq model has no configured quota")
        selected_profile = (
            _profile(required_profile) if required_profile is not None else None
        )
        if (
            selected_profile is not None
            and selected_profile not in self.policy.active_profiles
        ):
            raise GroqQuotaError("required Groq profile is not active")
        minute_start = _window_start(instant, _MINUTE_SECONDS)
        day_start = _window_start(instant, _DAY_SECONDS)

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM quota_reservations WHERE attempt_id = ?", (attempt,)
                ).fetchone()
                if existing is not None:
                    if (
                        existing["model_id"] != model
                        or existing["reserved_tokens"] != token_ceiling
                        or (
                            selected_profile is not None
                            and existing["profile"] != selected_profile
                        )
                    ):
                        raise GroqQuotaError(
                            "Groq attempt identity conflicts with durable reservation"
                        )
                    raise GroqAttemptAlreadyReserved(
                        "Groq attempt already crossed the durable pre-send boundary"
                    )

                candidates: list[tuple[int, int, str]] = []
                candidate_profiles = (
                    (selected_profile,)
                    if selected_profile is not None
                    else self.policy.active_profiles
                )
                for profile in candidate_profiles:
                    minute_requests, minute_tokens = self._used(
                        connection,
                        profile=profile,
                        model_id=model,
                        window_column="minute_window_start",
                        window_start=minute_start,
                    )
                    day_requests, day_tokens = self._used(
                        connection,
                        profile=profile,
                        model_id=model,
                        window_column="day_window_start",
                        window_start=day_start,
                    )
                    if (
                        minute_requests + 1 <= quota.requests_per_minute
                        and minute_tokens + token_ceiling <= quota.tokens_per_minute
                        and day_requests + 1 <= quota.requests_per_day
                        and day_tokens + token_ceiling <= quota.tokens_per_day
                    ):
                        candidates.append(
                            (
                                quota.tokens_per_day - day_tokens,
                                quota.tokens_per_minute - minute_tokens,
                                profile,
                            )
                        )
                if not candidates:
                    raise GroqQuotaExhausted("Groq quota is exhausted")
                # Greatest remaining daily/minute capacity, then stable alias.
                profile = sorted(candidates, key=lambda item: (-item[0], -item[1], item[2]))[0][2]
                reservation_id = "groq-quota-" + sha256(
                    (attempt + "\0" + profile + "\0" + model).encode("utf-8")
                ).hexdigest()
                created_at = instant.isoformat().replace("+00:00", "Z")
                connection.execute(
                    "INSERT INTO quota_reservations("
                    "attempt_id,reservation_id,profile,model_id,reserved_tokens,"
                    "minute_window_start,day_window_start,state,actual_tokens,"
                    "created_at,settled_at) VALUES (?,?,?,?,?,?,?,'RESERVED',NULL,?,NULL)",
                    (
                        attempt,
                        reservation_id,
                        profile,
                        model,
                        token_ceiling,
                        minute_start,
                        day_start,
                        created_at,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return GroqQuotaReservation(
            reservation_id,
            attempt,
            profile,
            model,
            token_ceiling,
            minute_start,
            day_start,
        )

    def settle(
        self,
        *,
        attempt_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        now: datetime,
    ) -> None:
        attempt = _identifier(attempt_id, "attempt_id")
        if type(prompt_tokens) is not int or prompt_tokens < 0:
            raise ValueError("prompt_tokens is invalid")
        if type(completion_tokens) is not int or completion_tokens < 0:
            raise ValueError("completion_tokens is invalid")
        actual = prompt_tokens + completion_tokens
        instant = _utc(now, "now").isoformat().replace("+00:00", "Z")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT state,reserved_tokens,actual_tokens FROM quota_reservations "
                    "WHERE attempt_id = ?",
                    (attempt,),
                ).fetchone()
                if row is None:
                    raise GroqQuotaError("Groq quota reservation is missing")
                if row["state"] == "SETTLED":
                    if row["actual_tokens"] != actual:
                        raise GroqQuotaError("Groq settlement conflicts with durable usage")
                    connection.commit()
                    return
                if row["state"] != "RESERVED":
                    raise GroqQuotaError("ambiguous Groq attempt cannot be settled")
                if actual > row["reserved_tokens"]:
                    raise GroqQuotaError("Groq usage exceeded reserved token ceiling")
                connection.execute(
                    "UPDATE quota_reservations SET state='SETTLED',actual_tokens=?,"
                    "settled_at=? WHERE attempt_id=? AND state='RESERVED'",
                    (actual, instant, attempt),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def mark_ambiguous(self, *, attempt_id: str) -> None:
        attempt = _identifier(attempt_id, "attempt_id")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT state FROM quota_reservations WHERE attempt_id = ?", (attempt,)
                ).fetchone()
                if row is None:
                    raise GroqQuotaError("Groq quota reservation is missing")
                if row["state"] == "SETTLED":
                    raise GroqQuotaError("settled Groq attempt cannot become ambiguous")
                connection.execute(
                    "UPDATE quota_reservations SET state='AMBIGUOUS' "
                    "WHERE attempt_id=? AND state='RESERVED'",
                    (attempt,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def record_provider_headers(
        self,
        *,
        attempt_id: str,
        headers: Mapping[str, str],
        observed_at: datetime,
    ) -> GroqProviderLimitObservation | None:
        """Persist sanitized diagnostics without changing local admission state."""

        attempt = _identifier(attempt_id, "attempt_id")
        observation = sanitize_groq_rate_limit_headers(
            headers, observed_at=observed_at
        )
        if observation is None:
            return None
        material = {
            "limit_requests": observation.limit_requests,
            "remaining_requests": observation.remaining_requests,
            "reset_requests": observation.reset_requests,
            "limit_tokens": observation.limit_tokens,
            "remaining_tokens": observation.remaining_tokens,
            "reset_tokens": observation.reset_tokens,
            "observed_at": observation.observed_at,
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                reservation = connection.execute(
                    "SELECT profile,model_id FROM quota_reservations WHERE attempt_id=?",
                    (attempt,),
                ).fetchone()
                if reservation is None:
                    raise GroqQuotaError("Groq quota reservation is missing")
                existing = connection.execute(
                    "SELECT observation_json FROM provider_limit_observations "
                    "WHERE attempt_id=?",
                    (attempt,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO provider_limit_observations("
                        "attempt_id,profile,model_id,observation_json) VALUES (?,?,?,?)",
                        (
                            attempt,
                            reservation["profile"],
                            reservation["model_id"],
                            encoded,
                        ),
                    )
                elif existing["observation_json"] != encoded:
                    raise GroqQuotaError(
                        "Groq provider-limit observation conflicts with durable evidence"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return observation

    def _latest_provider_observation(
        self,
        connection: sqlite3.Connection,
        *,
        profile: str,
        model_id: str,
    ) -> GroqProviderLimitObservation | None:
        row = connection.execute(
            "SELECT observation_json FROM provider_limit_observations "
            "WHERE profile=? AND model_id=? ORDER BY rowid DESC LIMIT 1",
            (profile, model_id),
        ).fetchone()
        if row is None:
            return None
        try:
            material = json.loads(row["observation_json"])
            return GroqProviderLimitObservation(**material)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise GroqQuotaError("Groq provider-limit evidence is invalid") from error

    def snapshot(
        self, *, profile: str, model_id: str, now: datetime
    ) -> GroqQuotaSnapshot:
        selected_profile = _profile(profile)
        model = _identifier(model_id, "model_id")
        if selected_profile not in self.policy.stored_profiles:
            raise GroqQuotaError("Groq profile is not configured")
        quota = self._quota_by_model.get(model)
        if quota is None:
            raise GroqQuotaError("Groq model has no configured quota")
        instant = _utc(now, "now")
        minute_start = _window_start(instant, _MINUTE_SECONDS)
        day_start = _window_start(instant, _DAY_SECONDS)
        with self._connection() as connection:
            minute_requests, minute_tokens = self._used(
                connection,
                profile=selected_profile,
                model_id=model,
                window_column="minute_window_start",
                window_start=minute_start,
            )
            day_requests, day_tokens = self._used(
                connection,
                profile=selected_profile,
                model_id=model,
                window_column="day_window_start",
                window_start=day_start,
            )
            provider_observation = self._latest_provider_observation(
                connection, profile=selected_profile, model_id=model
            )
        return GroqQuotaSnapshot(
            selected_profile,
            model,
            max(0, quota.requests_per_minute - minute_requests),
            max(0, quota.tokens_per_minute - minute_tokens),
            max(0, quota.requests_per_day - day_requests),
            max(0, quota.tokens_per_day - day_tokens),
            instant.isoformat().replace("+00:00", "Z"),
            provider_observation,
        )


InferenceCall = Callable[
    [ModelCallBinding, Callable[[], bool]], ModelCallObservation
]


class GroqQuotaBoundInference:
    """Single-dispatch wrapper around existing profile-bound Groq adapters."""

    def __init__(
        self,
        *,
        journal: DurableGroqQuotaJournal,
        model_id: str,
        prompt: str,
        maximum_output_tokens: int,
        calls_by_profile: Mapping[str, InferenceCall],
        required_profile: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if type(journal) is not DurableGroqQuotaJournal:
            raise TypeError("Groq quota journal is invalid")
        self.journal = journal
        self.model_id = _identifier(model_id, "model_id")
        if type(prompt) is not str or not prompt:
            raise ValueError("model prompt is invalid")
        self.prompt = prompt
        self.maximum_input_tokens = len(prompt.encode("utf-8"))
        self.maximum_output_tokens = _positive_int(
            maximum_output_tokens, "maximum_output_tokens"
        )
        if not isinstance(calls_by_profile, Mapping):
            raise TypeError("Groq profile calls must be a mapping")
        copied: dict[str, InferenceCall] = {}
        for profile, call in calls_by_profile.items():
            alias = _profile(profile)
            if alias not in journal.policy.active_profiles or not callable(call):
                raise ValueError("Groq profile call is not active")
            copied[alias] = call
        selected_profile = (
            _profile(required_profile) if required_profile is not None else None
        )
        if (
            selected_profile is not None
            and selected_profile not in journal.policy.active_profiles
        ):
            raise ValueError("required Groq profile is not active")
        expected_profiles = (
            {selected_profile}
            if selected_profile is not None
            else set(journal.policy.active_profiles)
        )
        if set(copied) != expected_profiles:
            raise ValueError("required Groq profile calls are incomplete")
        if not callable(clock):
            raise TypeError("Groq quota clock must be callable")
        self._calls = copied
        self._required_profile = selected_profile
        self._clock = clock

    def __call__(
        self,
        binding: ModelCallBinding,
        cancelled: Callable[[], bool],
    ) -> ModelCallObservation:
        if type(binding) is not ModelCallBinding:
            raise TypeError("durable model binding is required")
        if binding.provider_id != _PROVIDER or binding.model_id != self.model_id:
            raise GroqQuotaError("Groq model binding is invalid")
        reservation = self.journal.reserve(
            attempt_id=binding.attempt_id,
            model_id=self.model_id,
            maximum_input_tokens=self.maximum_input_tokens,
            maximum_output_tokens=self.maximum_output_tokens,
            now=self._clock(),
            required_profile=self._required_profile,
        )
        call = self._calls[reservation.profile]
        try:
            observation = call(binding, cancelled)
            if type(observation) is not ModelCallObservation:
                raise GroqQuotaError("Groq adapter returned invalid observation")
            output = observation.output
            if type(output) is not dict:
                raise GroqQuotaError("Groq usage is missing")
            prompt_tokens = output.get("prompt_tokens")
            completion_tokens = output.get("completion_tokens")
            provider_headers = output.get("provider_rate_limit_headers")
            if isinstance(provider_headers, Mapping):
                provider_observation = self.journal.record_provider_headers(
                    attempt_id=binding.attempt_id,
                    headers=provider_headers,
                    observed_at=self._clock(),
                )
                safe_output = dict(output)
                safe_output.pop("provider_rate_limit_headers", None)
                if provider_observation is not None:
                    safe_output["provider_rate_limits"] = {
                        "limit_requests": provider_observation.limit_requests,
                        "remaining_requests": provider_observation.remaining_requests,
                        "reset_requests": provider_observation.reset_requests,
                        "limit_tokens": provider_observation.limit_tokens,
                        "remaining_tokens": provider_observation.remaining_tokens,
                        "reset_tokens": provider_observation.reset_tokens,
                        "observed_at": provider_observation.observed_at,
                        "authoritative": False,
                    }
                observation = ModelCallObservation(
                    provider_id=observation.provider_id,
                    model_id=observation.model_id,
                    revision=observation.revision,
                    observed_at=observation.observed_at,
                    incurred_cost=observation.incurred_cost,
                    estimated_unbilled=observation.estimated_unbilled,
                    output=safe_output,
                    provider_request_id=observation.provider_request_id,
                    provider_response_id=observation.provider_response_id,
                    usage_id=observation.usage_id,
                    billing_id=observation.billing_id,
                )
            self.journal.settle(
                attempt_id=binding.attempt_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                now=self._clock(),
            )
            return observation
        except Exception:
            try:
                self.journal.mark_ambiguous(attempt_id=binding.attempt_id)
            except GroqQuotaError:
                pass
            raise
