"""Bounded, restart-safe Groq advisory research with no trading authority.

The advisory wave is deliberately outside order construction and dispatch.  It
accepts a bounded, secret-free research snapshot and asks four fixed roles for
analysis.  SQLite is the state authority and a regenerated JSONL file is the
human-readable event mirror.  An episode is marked ``STARTED`` durably before
the model boundary; any interrupted or failed call becomes ``UNKNOWN`` and is
never resent.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Callable, Mapping

from autotrade_foundation.local_filesystem import (
    LocalFilesystemQualificationError,
    freeze_local_filesystem_path,
    require_qualified_local_filesystem_path,
)

from .groq_account_pool import (
    DurableGroqQuotaJournal,
    GROQ_FREE_PLAN_QUOTAS_2026_10_20,
    GroqAccountCompliancePolicy,
    GroqQuotaBoundInference,
)
from .model_call import ModelCallBinding, ModelCallObservation
from .openai_compatible_model_adapter import (
    OpenAICompatibleInferenceAdapter,
    OpenAICompatibleSettings,
)


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SECRET_FIELD = re.compile(
    r"(?:(?:groq|mistral|provider|model)[_-])?"
    r"(?:api[_-]?key|secret|password|credential|authorization|bearer|private[_-]?key)s?",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r"(?i)(?:gsk_|sk-|bearer\s+)[A-Za-z0-9_./+=-]{8,}"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api.?key|secret|password|credential|authorization|private.?key)"
    r"\s*[:=]\s*)[^\s,;]{4,}"
)
_UTC_ZERO = "sha256:" + "0" * 64
_MAX_INPUT_JSON_BYTES = 4_500
_MAX_PROMPT_BYTES = 7_300
_MAX_OUTPUT_BYTES = 48_000
_MAX_OUTPUT_TOKENS = 600


class GroqAdvisoryError(RuntimeError):
    """Sanitized advisory-wave failure."""


@dataclass(frozen=True, slots=True)
class AdvisoryRole:
    ordinal: int
    role: str
    profile: str
    model_id: str
    instruction: str


ADVISORY_ROLES = (
    AdvisoryRole(
        1,
        "coordinator",
        "groq-account-1",
        "openai/gpt-oss-120b",
        "Synthesize a small set of falsifiable research hypotheses and rank the "
        "numeric evidence needed before any simulation experiment.",
    ),
    AdvisoryRole(
        2,
        "critic",
        "groq-account-2",
        "qwen/qwen3.8-27b",
        "Challenge the coordinator for leakage, overfitting, hidden costs, weak "
        "causality, and unsupported profit claims. Preserve useful hypotheses.",
    ),
    AdvisoryRole(
        3,
        "researcher",
        "groq-account-3",
        "openai/gpt-oss-20b",
        "Turn surviving hypotheses into bounded causal offline experiments with "
        "explicit train, validation, holdout, fees, spread, slippage and rejection rules.",
    ),
    AdvisoryRole(
        4,
        "evaluator",
        "groq-account-4",
        "openai/gpt-oss-120b",
        "Independently evaluate the other roles. Select only hypotheses that merit "
        "numeric ZERO-mode testing; never approve deployment or claim profitability.",
    ),
)


@dataclass(frozen=True, slots=True)
class AdvisoryEpisodeResult:
    role: str
    profile: str
    model_id: str
    status: str
    prompt_tokens: int | None
    completion_tokens: int | None
    output_text: str | None


@dataclass(frozen=True, slots=True)
class AdvisoryWaveResult:
    wave_id: str
    status: str
    episodes: tuple[AdvisoryEpisodeResult, ...]
    database_path: str
    jsonl_path: str


InferenceCall = Callable[
    [ModelCallBinding, Callable[[], bool]], ModelCallObservation
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("advisory clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("research input must be finite JSON") from error


def _contains_secret(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            type(key) is not str
            or _SECRET_FIELD.fullmatch(key) is not None
            or _contains_secret(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    if type(value) is str:
        return (
            _SECRET_TEXT.search(value) is not None
            or _SECRET_ASSIGNMENT.search(value) is not None
        )
    return False


def _sanitize_output(value: str) -> str:
    if type(value) is not str:
        raise GroqAdvisoryError("model output is invalid")
    sanitized = _SECRET_TEXT.sub("[REDACTED]", value)
    sanitized = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", sanitized)
    encoded = sanitized.encode("utf-8")
    if len(encoded) > _MAX_OUTPUT_BYTES:
        encoded = encoded[:_MAX_OUTPUT_BYTES]
        while True:
            try:
                sanitized = encoded.decode("utf-8")
                break
            except UnicodeDecodeError:
                encoded = encoded[:-1]
        sanitized += "\n[OUTPUT_TRUNCATED]"
    return sanitized


def _bounded_text(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    encoded = encoded[:limit]
    while True:
        try:
            return encoded.decode("utf-8") + "\n[INPUT_TRUNCATED]"
        except UnicodeDecodeError:
            encoded = encoded[:-1]


class DurableGroqAdvisoryWave:
    """Four-role advisory runner whose outputs have no execution authority."""

    _SCHEMA_VERSION = "1"

    def __init__(
        self,
        state_directory: str | Path,
        *,
        calls_by_role: Mapping[str, InferenceCall] | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if type(state_directory) not in (str, type(Path())):
            raise TypeError("advisory state directory must be text or Path")
        frozen = Path(
            freeze_local_filesystem_path(Path(state_directory).expanduser())
        )
        try:
            require_qualified_local_filesystem_path(frozen)
        except LocalFilesystemQualificationError as error:
            raise GroqAdvisoryError(
                "advisory state requires a qualified local filesystem"
            ) from error
        if not callable(clock):
            raise TypeError("advisory clock must be callable")
        frozen.mkdir(parents=True, exist_ok=True)
        self.state_directory = frozen
        self.database_path = frozen / "groq-advisory.sqlite3"
        self.jsonl_path = frozen / "groq-advisory-events.jsonl"
        self._clock = clock
        expected_roles = {item.role for item in ADVISORY_ROLES}
        if calls_by_role is None:
            self._calls_by_role: dict[str, InferenceCall] | None = None
        else:
            if not isinstance(calls_by_role, Mapping):
                raise TypeError("advisory role calls must be a mapping")
            copied = dict(calls_by_role)
            if set(copied) != expected_roles or any(
                not callable(call) for call in copied.values()
            ):
                raise ValueError("every advisory role requires exactly one call")
            self._calls_by_role = copied
        self._initialize()
        self._recover_interrupted()
        self._sync_jsonl()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=30, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
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
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS advisory_metadata("
                "key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS advisory_waves("
                "wave_id TEXT PRIMARY KEY,input_json TEXT NOT NULL,"
                "input_digest TEXT NOT NULL,status TEXT NOT NULL "
                "CHECK(status IN ('RUNNING','COMPLETED','UNKNOWN')),"
                "created_at TEXT NOT NULL,updated_at TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS advisory_episodes("
                "wave_id TEXT NOT NULL,ordinal INTEGER NOT NULL,role TEXT NOT NULL,"
                "profile TEXT NOT NULL,model_id TEXT NOT NULL,attempt_id TEXT NOT NULL UNIQUE,"
                "prompt TEXT NOT NULL,prompt_digest TEXT NOT NULL,status TEXT NOT NULL "
                "CHECK(status IN ('PREPARED','STARTED','OBSERVED','UNKNOWN')),"
                "output_text TEXT,prompt_tokens INTEGER,completion_tokens INTEGER,"
                "provider_response_id TEXT,error_code TEXT,started_at TEXT,completed_at TEXT,"
                "PRIMARY KEY(wave_id,ordinal),"
                "FOREIGN KEY(wave_id) REFERENCES advisory_waves(wave_id));"
                "CREATE TABLE IF NOT EXISTS advisory_events("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT,event_json TEXT NOT NULL);"
            )
            existing = connection.execute(
                "SELECT value FROM advisory_metadata WHERE key='schema_version'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO advisory_metadata(key,value) VALUES('schema_version',?)",
                    (self._SCHEMA_VERSION,),
                )
            elif existing["value"] != self._SCHEMA_VERSION:
                raise GroqAdvisoryError("advisory database schema is unsupported")

    def _event(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        wave_id: str,
        role: str | None,
        status: str,
        observed_at: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        material = {
            "details": dict(details or {}),
            "event_type": event_type,
            "observed_at": observed_at,
            "role": role,
            "status": status,
            "wave_id": wave_id,
        }
        connection.execute(
            "INSERT INTO advisory_events(event_json) VALUES(?)",
            (_canonical_json(material),),
        )

    def _recover_interrupted(self) -> None:
        observed_at = _utc_text(self._clock())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                interrupted = connection.execute(
                    "SELECT wave_id,role FROM advisory_episodes WHERE status='STARTED'"
                ).fetchall()
                for row in interrupted:
                    connection.execute(
                        "UPDATE advisory_episodes SET status='UNKNOWN',"
                        "error_code='INTERRUPTED_AFTER_START',completed_at=? "
                        "WHERE wave_id=? AND role=? AND status='STARTED'",
                        (observed_at, row["wave_id"], row["role"]),
                    )
                    connection.execute(
                        "UPDATE advisory_waves SET status='UNKNOWN',updated_at=? "
                        "WHERE wave_id=?",
                        (observed_at, row["wave_id"]),
                    )
                    self._event(
                        connection,
                        event_type="AdvisoryEpisodeRecoveredUnknown",
                        wave_id=row["wave_id"],
                        role=row["role"],
                        status="UNKNOWN",
                        observed_at=observed_at,
                        details={"error_code": "INTERRUPTED_AFTER_START"},
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _sync_jsonl(self) -> None:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sequence,event_json FROM advisory_events ORDER BY sequence"
            ).fetchall()
        temporary = self.jsonl_path.with_suffix(".jsonl.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                event = json.loads(row["event_json"])
                stream.write(
                    _canonical_json({"sequence": row["sequence"], **event}) + "\n"
                )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.jsonl_path)

    @staticmethod
    def _prompt(
        role: AdvisoryRole,
        input_json: str,
        previous: tuple[tuple[str, str], ...],
    ) -> str:
        prior = "\n\n".join(
            f"PRIOR ROLE {name.upper()}:\n{_bounded_text(output, 500)}"
            for name, output in previous
        )
        prompt = (
            "AUTOTRADE RESEARCH-ONLY ADVISORY. You have no trading, order, account, "
            "configuration, tool, network-action, or deployment authority. Do not request "
            "or reproduce credentials. Your text is an untrusted hypothesis that must be "
            "validated independently by causal numeric simulation.\n\n"
            f"ROLE: {role.role}\nTASK: {role.instruction}\n\n"
            f"BOUNDED RESEARCH SNAPSHOT:\n{input_json}\n\n"
            f"{prior}\n\n"
            "Respond concisely with assumptions, evidence gaps, proposed tests, rejection "
            "conditions, and resource cost. Never issue BUY/SELL instructions, claim proven "
            "profit, or authorize any real/paper order."
        )
        if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
            raise GroqAdvisoryError("advisory prompt exceeds its fixed bound")
        return prompt

    def _role_call(self, role: AdvisoryRole, prompt: str) -> InferenceCall:
        if self._calls_by_role is not None:
            return self._calls_by_role[role.role]
        settings = OpenAICompatibleSettings(
            provider_id="groq",
            profile=role.profile,
            model_id=role.model_id,
            maximum_input_bytes=_MAX_PROMPT_BYTES,
            maximum_response_bytes=256_000,
            maximum_output_tokens=_MAX_OUTPUT_TOKENS,
            input_usd_per_million=Decimal(0),
            output_usd_per_million=Decimal(0),
            timeout_seconds=60,
        )
        adapter = OpenAICompatibleInferenceAdapter(settings)

        def call(
            binding: ModelCallBinding,
            cancelled: Callable[[], bool],
        ) -> ModelCallObservation:
            return adapter.inference(binding, cancelled, prompt=prompt)

        return call

    def _quota_bound_call(
        self, role: AdvisoryRole, prompt: str
    ) -> GroqQuotaBoundInference:
        journal = DurableGroqQuotaJournal(
            self.state_directory / "groq-quota.sqlite3",
            policy=GroqAccountCompliancePolicy(),
            quotas=GROQ_FREE_PLAN_QUOTAS_2026_10_20,
        )
        return GroqQuotaBoundInference(
            journal=journal,
            model_id=role.model_id,
            prompt=prompt,
            maximum_output_tokens=_MAX_OUTPUT_TOKENS,
            calls_by_profile={role.profile: self._role_call(role, prompt)},
            required_profile=role.profile,
            clock=self._clock,
        )

    @staticmethod
    def _binding(
        *, role: AdvisoryRole, wave_id: str, prompt: str, created_at: str
    ) -> ModelCallBinding:
        prompt_digest = "sha256:" + sha256(prompt.encode("utf-8")).hexdigest()
        attempt_hash = sha256(
            (wave_id + "\0" + role.role + "\0" + prompt_digest).encode("utf-8")
        ).hexdigest()
        attempt_id = "groq-advisory-" + attempt_hash
        return ModelCallBinding(
            attempt_id=attempt_id,
            request_id=attempt_id,
            job_id=f"{wave_id}:{role.role}",
            input_digest=prompt_digest,
            provider_id="groq",
            model_id=role.model_id,
            revision="groq-advisory-v1",
            remote=True,
            reserved_cost=Decimal(0),
            pricing_evidence_id="groq-free-plan-2026-10-20",
            pricing_evidence_digest=_UTC_ZERO,
            pricing_as_of=created_at,
            cost_currency="USD",
            result_schema_id="autotrade.groq-advisory.text.v1",
            fallback_parent_attempt_id=None,
            fallback_index=0,
        )

    def _ensure_wave(self, wave_id: str, input_json: str) -> str:
        input_digest = "sha256:" + sha256(input_json.encode("utf-8")).hexdigest()
        now = _utc_text(self._clock())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT input_digest,created_at FROM advisory_waves WHERE wave_id=?",
                    (wave_id,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO advisory_waves(wave_id,input_json,input_digest,status,"
                        "created_at,updated_at) VALUES(?,?,?,'RUNNING',?,?)",
                        (wave_id, input_json, input_digest, now, now),
                    )
                    self._event(
                        connection,
                        event_type="AdvisoryWaveCreated",
                        wave_id=wave_id,
                        role=None,
                        status="RUNNING",
                        observed_at=now,
                        details={
                            "input_digest": input_digest,
                            "input_json": input_json,
                        },
                    )
                    created_at = now
                else:
                    if existing["input_digest"] != input_digest:
                        raise GroqAdvisoryError(
                            "advisory wave identity conflicts with durable input"
                        )
                    created_at = existing["created_at"]
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._sync_jsonl()
        return created_at

    def _prepare_episode(
        self,
        *,
        wave_id: str,
        role: AdvisoryRole,
        prompt: str,
        binding: ModelCallBinding,
    ) -> str:
        prompt_digest = binding.input_digest
        now = _utc_text(self._clock())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT status,prompt_digest,attempt_id FROM advisory_episodes "
                    "WHERE wave_id=? AND ordinal=?",
                    (wave_id, role.ordinal),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO advisory_episodes(wave_id,ordinal,role,profile,model_id,"
                        "attempt_id,prompt,prompt_digest,status) "
                        "VALUES(?,?,?,?,?,?,?,?,'PREPARED')",
                        (
                            wave_id,
                            role.ordinal,
                            role.role,
                            role.profile,
                            role.model_id,
                            binding.attempt_id,
                            prompt,
                            prompt_digest,
                        ),
                    )
                    self._event(
                        connection,
                        event_type="AdvisoryEpisodePrepared",
                        wave_id=wave_id,
                        role=role.role,
                        status="PREPARED",
                        observed_at=now,
                        details={
                            "attempt_id": binding.attempt_id,
                            "model_id": role.model_id,
                            "profile": role.profile,
                            "prompt": prompt,
                            "prompt_digest": prompt_digest,
                        },
                    )
                    status = "PREPARED"
                else:
                    if (
                        existing["prompt_digest"] != prompt_digest
                        or existing["attempt_id"] != binding.attempt_id
                    ):
                        raise GroqAdvisoryError(
                            "advisory episode conflicts with durable prompt"
                        )
                    status = existing["status"]
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._sync_jsonl()
        return status

    def _mark_started(self, wave_id: str, role: AdvisoryRole) -> None:
        now = _utc_text(self._clock())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                changed = connection.execute(
                    "UPDATE advisory_episodes SET status='STARTED',started_at=? "
                    "WHERE wave_id=? AND ordinal=? AND status='PREPARED'",
                    (now, wave_id, role.ordinal),
                ).rowcount
                if changed != 1:
                    raise GroqAdvisoryError("advisory episode cannot cross start boundary")
                self._event(
                    connection,
                    event_type="AdvisoryEpisodeStarted",
                    wave_id=wave_id,
                    role=role.role,
                    status="STARTED",
                    observed_at=now,
                    details={"ordinal": role.ordinal},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._sync_jsonl()

    def _mark_observed(
        self,
        *,
        wave_id: str,
        role: AdvisoryRole,
        observation: ModelCallObservation,
    ) -> None:
        if type(observation) is not ModelCallObservation or type(observation.output) is not dict:
            raise GroqAdvisoryError("advisory observation is invalid")
        output_text = _sanitize_output(observation.output.get("text"))
        prompt_tokens = observation.output.get("prompt_tokens")
        completion_tokens = observation.output.get("completion_tokens")
        if (
            type(prompt_tokens) is not int
            or prompt_tokens < 0
            or type(completion_tokens) is not int
            or completion_tokens < 0
            or completion_tokens > _MAX_OUTPUT_TOKENS
        ):
            raise GroqAdvisoryError("advisory usage is invalid")
        now = _utc_text(self._clock())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                changed = connection.execute(
                    "UPDATE advisory_episodes SET status='OBSERVED',output_text=?,"
                    "prompt_tokens=?,completion_tokens=?,provider_response_id=?,completed_at=? "
                    "WHERE wave_id=? AND ordinal=? AND status='STARTED'",
                    (
                        output_text,
                        prompt_tokens,
                        completion_tokens,
                        observation.provider_response_id,
                        now,
                        wave_id,
                        role.ordinal,
                    ),
                ).rowcount
                if changed != 1:
                    raise GroqAdvisoryError("advisory observation has no started episode")
                self._event(
                    connection,
                    event_type="AdvisoryEpisodeObserved",
                    wave_id=wave_id,
                    role=role.role,
                    status="OBSERVED",
                    observed_at=now,
                    details={
                        "completion_tokens": completion_tokens,
                        "output_text": output_text,
                        "prompt_tokens": prompt_tokens,
                        "provider_response_id": observation.provider_response_id,
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._sync_jsonl()

    def _mark_unknown(self, wave_id: str, role: AdvisoryRole) -> None:
        now = _utc_text(self._clock())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE advisory_episodes SET status='UNKNOWN',"
                    "error_code='CALL_OUTCOME_UNKNOWN',completed_at=? "
                    "WHERE wave_id=? AND ordinal=? AND status='STARTED'",
                    (now, wave_id, role.ordinal),
                )
                connection.execute(
                    "UPDATE advisory_waves SET status='UNKNOWN',updated_at=? WHERE wave_id=?",
                    (now, wave_id),
                )
                self._event(
                    connection,
                    event_type="AdvisoryEpisodeUnknown",
                    wave_id=wave_id,
                    role=role.role,
                    status="UNKNOWN",
                    observed_at=now,
                    details={"error_code": "CALL_OUTCOME_UNKNOWN"},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._sync_jsonl()

    def _complete(self, wave_id: str) -> None:
        now = _utc_text(self._clock())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                statuses = connection.execute(
                    "SELECT status FROM advisory_episodes WHERE wave_id=? ORDER BY ordinal",
                    (wave_id,),
                ).fetchall()
                if len(statuses) != len(ADVISORY_ROLES) or any(
                    row["status"] != "OBSERVED" for row in statuses
                ):
                    raise GroqAdvisoryError("advisory wave cannot complete")
                connection.execute(
                    "UPDATE advisory_waves SET status='COMPLETED',updated_at=? WHERE wave_id=?",
                    (now, wave_id),
                )
                self._event(
                    connection,
                    event_type="AdvisoryWaveCompleted",
                    wave_id=wave_id,
                    role=None,
                    status="COMPLETED",
                    observed_at=now,
                    details={"episode_count": len(ADVISORY_ROLES)},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._sync_jsonl()

    def _result(self, wave_id: str) -> AdvisoryWaveResult:
        with self._connection() as connection:
            wave = connection.execute(
                "SELECT status FROM advisory_waves WHERE wave_id=?", (wave_id,)
            ).fetchone()
            if wave is None:
                raise GroqAdvisoryError("advisory wave is unavailable")
            rows = connection.execute(
                "SELECT role,profile,model_id,status,prompt_tokens,completion_tokens,"
                "output_text FROM advisory_episodes WHERE wave_id=? ORDER BY ordinal",
                (wave_id,),
            ).fetchall()
        episodes = tuple(AdvisoryEpisodeResult(**dict(row)) for row in rows)
        return AdvisoryWaveResult(
            wave_id,
            wave["status"],
            episodes,
            str(self.database_path),
            str(self.jsonl_path),
        )

    def run(self, *, wave_id: str, research_input: object) -> AdvisoryWaveResult:
        wave = _identifier(wave_id, "wave_id")
        if _contains_secret(research_input):
            raise ValueError("research input contains secret-like material")
        input_json = _canonical_json(research_input)
        if len(input_json.encode("utf-8")) > _MAX_INPUT_JSON_BYTES:
            raise ValueError("research input exceeds its fixed bound")
        created_at = self._ensure_wave(wave, input_json)
        current = self._result(wave)
        if current.status in ("COMPLETED", "UNKNOWN"):
            return current

        previous: list[tuple[str, str]] = [
            (episode.role, episode.output_text or "")
            for episode in current.episodes
            if episode.status == "OBSERVED"
        ]
        for role in ADVISORY_ROLES:
            existing = next(
                (episode for episode in current.episodes if episode.role == role.role),
                None,
            )
            if existing is not None:
                if existing.status == "OBSERVED":
                    continue
                if existing.status != "PREPARED":
                    return self._result(wave)
            prompt = self._prompt(role, input_json, tuple(previous))
            binding = self._binding(
                role=role, wave_id=wave, prompt=prompt, created_at=created_at
            )
            status = self._prepare_episode(
                wave_id=wave, role=role, prompt=prompt, binding=binding
            )
            if status != "PREPARED":
                return self._result(wave)
            inference = self._quota_bound_call(role, prompt)
            self._mark_started(wave, role)
            try:
                observation = inference(binding, lambda: False)
                self._mark_observed(
                    wave_id=wave, role=role, observation=observation
                )
            except Exception:
                self._mark_unknown(wave, role)
                return self._result(wave)
            observed = self._result(wave)
            episode = next(item for item in observed.episodes if item.role == role.role)
            previous.append((role.role, episode.output_text or ""))
            current = observed
        self._complete(wave)
        return self._result(wave)


def advisory_result_json(result: AdvisoryWaveResult) -> str:
    """Stable, non-secret CLI projection."""

    return _canonical_json(
        {
            "database_path": result.database_path,
            "episodes": [asdict(item) for item in result.episodes],
            "jsonl_path": result.jsonl_path,
            "status": result.status,
            "wave_id": result.wave_id,
        }
    )
