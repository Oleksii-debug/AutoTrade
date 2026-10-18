"""Fail-closed qualification of an existing historical campaign workspace.

This tool is deliberately read-only.  It does not run a campaign, repair
evidence, contact a provider, or turn missing evidence into a passing claim.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any
from zipfile import BadZipFile, ZipFile

from mvp.autotrade_mvp.adaptive_ma import verify_model


SCHEMA = "autotrade-historical-e2e-qualification-v1"
IDENTITY_SCHEMA = "autotrade-historical-e2e-identity-v1"
RESTART_SCHEMA = "autotrade-historical-e2e-restart-v1"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTIC_ZIP_BYTES = 64 * 1024 * 1024
MAX_SCANNED_FILES = 2_000
_HEX = re.compile(r"^[0-9a-f]+$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(
        r"(?:api[_-]?key|api[_-]?secret|access[_-]?token|refresh[_-]?token|"
        r"client[_-]?secret|password|private[_-]?key)\s*[\"']?\s*[:=]\s*"
        r"[\"']?(?!(?:none|null|redacted|unavailable|not_configured)\b|\*{3})"
        r"[A-Za-z0-9+/=_-]{8,}",
        re.I,
    ),
)


class QualificationError(ValueError):
    """A present artifact contradicts the qualification contract."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_digest(value: object, length: int = 64) -> bool:
    return type(value) is str and len(value) == length and _HEX.fullmatch(value) is not None


def _is_source_sha(value: object) -> bool:
    return type(value) is str and len(value) in {40, 64} and _HEX.fullmatch(value) is not None


def _reject_alias(path: Path, root: Path, *, must_exist: bool = True) -> None:
    """Reject symlinks and paths escaping the caller-selected workspace."""
    root_resolved = root.resolve(strict=True)
    candidate = path.absolute()
    if must_exist and not candidate.exists():
        raise FileNotFoundError(candidate)
    relative = candidate.relative_to(root.absolute())
    cursor = root.absolute()
    if root.is_symlink():
        raise QualificationError("workspace is redirected")
    for part in relative.parts:
        cursor /= part
        if cursor.exists() or cursor.is_symlink():
            if cursor.is_symlink():
                raise QualificationError("workspace evidence is redirected")
    if candidate.exists() and not candidate.resolve(strict=True).is_relative_to(root_resolved):
        raise QualificationError("workspace evidence escapes its root")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QualificationError("JSON object contains a duplicate key")
        result[key] = value
    return result


def _load_json(path: Path, root: Path) -> dict[str, Any]:
    _reject_alias(path, root)
    if not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise QualificationError("JSON evidence is not a bounded ordinary file")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                QualificationError(f"non-finite JSON number: {value}")),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError("JSON evidence is malformed") from exc
    if type(value) is not dict:
        raise QualificationError("JSON evidence root must be an object")
    return value


def _month(value: object) -> int:
    if (type(value) is not str or len(value) != 7 or value[4] != "-"
            or not value[:4].isdigit() or not value[5:].isdigit()):
        raise QualificationError("campaign period is not exact YYYY-MM")
    year, month = int(value[:4]), int(value[5:])
    if not 2024 <= year <= 2100 or not 1 <= month <= 12:
        raise QualificationError("campaign period is outside the supported calendar")
    return year * 12 + month


def _check(checks: list[dict[str, str]], check_id: str, status: str,
           summary: str) -> None:
    if status not in {"PASS", "FAIL", "INCOMPLETE"}:
        raise AssertionError("invalid qualification status")
    checks.append({"id": check_id, "status": status, "summary": summary})


def _verify_self_digest(value: dict[str, Any], field: str, label: str) -> str:
    digest = value.get(field)
    if not _is_digest(digest):
        raise QualificationError(f"{label} has no canonical SHA-256 identity")
    payload = {key: item for key, item in value.items() if key != field}
    if sha256(_canonical(payload)).hexdigest() != digest:
        raise QualificationError(f"{label} content digest differs")
    return digest


def _identity(root: Path, expected_source_sha: str, expected_build_sha256: str,
              checks: list[dict[str, str]]) -> dict[str, Any] | None:
    path = root / "qualification" / "identity.json"
    if not path.exists():
        _check(checks, "exact_identity", "INCOMPLETE",
               "qualification/identity.json is missing")
        return None
    try:
        value = _load_json(path, root)
        required = {"schema", "source_sha", "build_sha256", "mode",
                    "provider_credentials", "real_order_route", "network_orders",
                    "immutable_artifacts"}
        if not required.issubset(value):
            raise QualificationError("identity evidence lacks required fields")
        if (value["schema"] != IDENTITY_SCHEMA
                or value["source_sha"] != expected_source_sha
                or value["build_sha256"] != expected_build_sha256):
            raise QualificationError("source or build identity differs from the requested candidate")
        if (value["mode"] != "ZERO" or value["provider_credentials"] != "NONE"
                or value["real_order_route"] != "UNAVAILABLE"
                or value["network_orders"] is not False):
            raise QualificationError("workspace is not bound to credential-free ZERO routing")
        artifacts = value["immutable_artifacts"]
        expected_paths = sorted(
            path for directory in ("models", "validation", "reports")
            if (root / directory).is_dir()
            for path in (root / directory).glob("*.json")
        )
        expected_names = {path.relative_to(root).as_posix() for path in expected_paths}
        if type(artifacts) is not dict or set(artifacts) != expected_names:
            raise QualificationError("identity does not enumerate every immutable campaign artifact")
        for path in expected_paths:
            relative = path.relative_to(root).as_posix()
            if not _is_digest(artifacts[relative]) or artifacts[relative] != _sha256_file(path):
                raise QualificationError("immutable campaign artifact digest differs")
    except (OSError, QualificationError, FileNotFoundError) as exc:
        _check(checks, "exact_identity", "FAIL", str(exc))
        return None
    _check(checks, "exact_identity", "PASS",
           "source, build and simulation-only route identity match")
    return value


def _learning_artifacts(root: Path, checks: list[dict[str, str]]) -> tuple[
        dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    models: dict[str, dict[str, Any]] = {}
    validations: dict[str, dict[str, Any]] = {}
    model_paths = sorted((root / "models").glob("*.json")) if (root / "models").is_dir() else []
    validation_paths = (sorted((root / "validation").glob("*.json"))
                        if (root / "validation").is_dir() else [])
    if not model_paths or not validation_paths:
        _check(checks, "learning_artifacts", "INCOMPLETE",
               "a frozen model and independent validation report are both required")
        return models, validations
    try:
        for path in model_paths:
            model = _load_json(path, root)
            digest = _verify_self_digest(model, "model_sha256", "trained model")
            if (not _is_digest(model.get("source_dataset_sha256"))
                    or (model.get("schema") == "autotrade-causal-ma-training-v1"
                        and model.get("economic_edge_status") !=
                        "NOT_EVALUATED_USE_INDEPENDENT_HOLDOUT")
                    or model.get("schema") not in {
                        "autotrade-causal-ma-training-v1",
                        "autotrade-closed-simulation-training-v2",
                    }):
                raise QualificationError("trained model provenance or claim scope differs")
            models[digest] = model
        for path in validation_paths:
            report = _load_json(path, root)
            digest = _verify_self_digest(report, "report_sha256", "validation report")
            model = models.get(report.get("model_sha256"))
            if (model is None or not _is_digest(report.get("validation_dataset_sha256"))
                    or report.get("model_updated") is not False
                    or report.get("status") !=
                    "INDEPENDENT_DIRECTIONAL_VALIDATION_NOT_TRADING_PERFORMANCE"):
                raise QualificationError("validation is not bound to a frozen model")
            if model.get("schema") == "autotrade-closed-simulation-training-v2":
                try:
                    validation_start = report["validation_start_open_utc"]
                    verify_model(
                        model,
                        symbol=report["symbol"],
                        interval=report["interval"],
                        holdout_start_open=datetime.fromisoformat(validation_start),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise QualificationError(
                        "trained model does not satisfy its production verifier"
                    ) from exc
            if (model["source_dataset_sha256"] == report["validation_dataset_sha256"]
                    or _month(model["training_last_close_utc"][:7]) >
                    _month(report["validation_start_open_utc"][:7])):
                raise QualificationError("training and validation are not separated")
            validations[digest] = report
    except (OSError, KeyError, QualificationError, FileNotFoundError) as exc:
        _check(checks, "learning_artifacts", "FAIL", str(exc))
        return {}, {}
    _check(checks, "learning_artifacts", "PASS",
           "frozen learning and no-update validation artifacts are integrity-bound")
    return models, validations


_ACCOUNT_FIELDS = {
    "status", "completed_episodes", "cash_usd", "position",
    "agent_mark_pnl_usd", "agent_max_mark_drawdown_fraction",
    "cash_baseline_equity_usd", "buy_hold_liquidation_scenario_pnl_usd",
    "missed_fill_metrics", "causal_regime_analysis", "conservative_uncertainty",
    "dataset_identity_sha256",
}


def _campaign_reports(root: Path, models: dict[str, dict[str, Any]],
                      validations: dict[str, dict[str, Any]],
                      checks: list[dict[str, str]]) -> list[dict[str, Any]]:
    report_paths = (sorted((root / "reports").glob("*.json"))
                    if (root / "reports").is_dir() else [])
    if not report_paths:
        _check(checks, "campaign_reports", "INCOMPLETE",
               "no completed campaign report is present")
        _check(checks, "dataset_separation", "INCOMPLETE",
               "holdout dataset binding is not yet available")
        return []
    reports: list[dict[str, Any]] = []
    report_keys: set[tuple[str, str, str]] = set()
    completed_keys: set[tuple[str, str, str]] = set()
    durable_accounts_missing = False
    try:
        for path in report_paths:
            report = _load_json(path, root)
            if (report.get("status") !=
                    "INTERNAL_TWO_ACCOUNT_HISTORICAL_CUT_NOT_PRODUCT_QUALIFICATION"):
                raise QualificationError("campaign report claim scope differs")
            periods = report.get("periods")
            if type(periods) is not dict or set(periods) != {"training", "validation", "holdout"}:
                raise QualificationError("campaign split declaration is malformed")
            values = []
            for name in ("training", "validation", "holdout"):
                span = periods[name]
                if type(span) is not list or len(span) != 2:
                    raise QualificationError("campaign split span is malformed")
                values.extend(_month(item) for item in span)
            if not (values[0] <= values[1] < values[2] <= values[3] < values[4] <= values[5]):
                raise QualificationError("training, validation and holdout periods overlap")
            symbol = report.get("symbol")
            if symbol not in {"BTCUSDT", "ETHUSDT"} or report.get("interval") != "1h":
                raise QualificationError("campaign instrument identity differs")
            holdout_from, holdout_through = periods["holdout"]
            report_key = (symbol, holdout_from, holdout_through)
            report_keys.add(report_key)
            model = models.get(report.get("model_sha256"))
            validation = validations.get(report.get("validation_report_sha256"))
            if model is None or validation is None or validation.get("model_sha256") != report.get("model_sha256"):
                raise QualificationError("campaign is not bound to its model and validation")
            accounts = report.get("accounts")
            if type(accounts) is not dict or set(accounts) != {"200", "1000"}:
                raise QualificationError("campaign must retain separate USD 200 and USD 1000 accounts")
            holdout_digests: set[str] = set()
            report_completed = True
            bounded = report.get("qualification_cut")
            if bounded is not None:
                if (type(bounded) is not dict
                        or bounded.get("scope") !=
                        "BOUNDED_CAUSAL_PREFIX_NOT_FULL_MONTH_PERFORMANCE"
                        or type(bounded.get("episode_limit")) is not int
                        or bounded["episode_limit"] < 12
                        or bounded.get("all_accounts_reached_limit") is not True):
                    raise QualificationError("bounded campaign cut declaration is malformed")
            for budget, account in accounts.items():
                if type(account) is not dict or not _ACCOUNT_FIELDS.issubset(account):
                    raise QualificationError("campaign account evidence is incomplete")
                if account["status"] == "BOUNDED_CAUSAL_PREFIX_COMPLETED_NOT_FULL_MONTH":
                    if (bounded is None
                            or account.get("runtime_status") != "PAUSED"
                            or account["completed_episodes"] != bounded["episode_limit"]):
                        raise QualificationError(
                            "bounded account cut is not bound to its paused runtime prefix"
                        )
                elif account["status"] != "COMPLETED":
                    if account["status"] in {"PAUSED", "RUNNING"}:
                        report_completed = False
                    else:
                        raise QualificationError("campaign account ended in an unsafe state")
                if type(account["completed_episodes"]) is not int or account["completed_episodes"] < 1:
                    raise QualificationError("campaign account has no completed episodes")
                digest = account["dataset_identity_sha256"]
                if not _is_digest(digest):
                    raise QualificationError("holdout dataset identity is malformed")
                holdout_digests.add(digest)
                account_path = (root / "accounts" / symbol /
                                f"{holdout_from}-{holdout_through}" / f"usd-{budget}")
                if not account_path.is_dir():
                    durable_accounts_missing = True
            if len(holdout_digests) != 1:
                raise QualificationError("two accounts do not share one immutable holdout")
            train_digest = model["source_dataset_sha256"]
            validation_digest = validation["validation_dataset_sha256"]
            if len({train_digest, validation_digest, *holdout_digests}) != 3:
                raise QualificationError("train, validation and holdout datasets are not distinct")
            if report_completed:
                completed_keys.add(report_key)
            reports.append(report)
    except (OSError, KeyError, QualificationError, FileNotFoundError) as exc:
        _check(checks, "campaign_reports", "FAIL", str(exc))
        _check(checks, "dataset_separation", "FAIL",
               "campaign dataset binding cannot be trusted")
        return []
    incomplete = report_keys != completed_keys or durable_accounts_missing
    _check(checks, "campaign_reports", "INCOMPLETE" if incomplete else "PASS",
           "a campaign cut is still paused or lacks its durable account workspace" if incomplete else
           "accounting, risk, execution-cost and comparative report fields are present; "
           "any bounded cut is explicitly not a full-month performance claim")
    _check(checks, "dataset_separation", "PASS",
           "training, validation and holdout periods and digests are distinct")
    return reports


def _account_directories(root: Path) -> list[Path]:
    accounts = root / "accounts"
    if not accounts.is_dir():
        return []
    return sorted(path for path in accounts.glob("*/*/usd-*") if path.is_dir())


def _journal_value_is_safe(value: object) -> bool:
    if type(value) is dict:
        for key, item in value.items():
            lowered = key.lower().replace("-", "_") if type(key) is str else ""
            if lowered in {"api_key", "api_secret", "access_token", "refresh_token",
                           "client_secret", "password", "private_key", "credential"}:
                if item is not None and not (
                        type(item) is str and item in {
                            "NONE", "UNAVAILABLE", "NOT_CONFIGURED", "REDACTED"}):
                    return False
            if lowered in {"environment", "mode", "order_mode"} and type(item) is str:
                if item.upper() in {"LIVE", "REAL", "PAPER"}:
                    return False
            if lowered in {"real_order_route", "order_route"}:
                if item is not None and item is not False and not (
                        type(item) is str and item in {
                            "NONE", "UNAVAILABLE", "NOT_CONFIGURED"}):
                    return False
            if not _journal_value_is_safe(item):
                return False
        return True
    if type(value) is list:
        return all(_journal_value_is_safe(item) for item in value)
    return True


def _read_journal(path: Path, root: Path) -> tuple[int, set[str]]:
    _reject_alias(path, root)
    if not path.is_file() or path.stat().st_size > 512 * 1024 * 1024:
        raise QualificationError("account journal is not a bounded ordinary file")
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise QualificationError("account journal integrity check failed")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            if not {"event_type", "journal_sequence", "payload_json"}.issubset(columns):
                raise QualificationError("account journal schema differs")
            count, low, high, distinct = connection.execute(
                "SELECT COUNT(*), MIN(journal_sequence), MAX(journal_sequence), "
                "COUNT(DISTINCT journal_sequence) FROM events"
            ).fetchone()
            if type(count) is not int or count < 1 or (low, high, distinct) != (1, count, count):
                raise QualificationError("account journal sequence is not contiguous")
            event_types = {row[0] for row in connection.execute(
                "SELECT DISTINCT event_type FROM events") if type(row[0]) is str}
            for row in connection.execute("SELECT payload_json FROM events"):
                try:
                    payload = json.loads(row[0], object_pairs_hook=_strict_object)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise QualificationError("account journal payload is malformed") from exc
                if not _journal_value_is_safe(payload):
                    raise QualificationError("account journal contains credentials or a real route")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise QualificationError("account journal cannot be read safely") from exc
    return count, event_types


def _runtime_evidence(root: Path, expected_build_sha256: str,
                      checks: list[dict[str, str]]) -> tuple[list[Path], dict[str, str], dict[str, int]]:
    accounts = _account_directories(root)
    if not accounts:
        _check(checks, "durable_runtime", "INCOMPLETE", "no durable account workspace exists")
        _check(checks, "financial_journals", "INCOMPLETE", "no account journal exists")
        return [], {}, {}
    checkpoint_hashes: dict[str, str] = {}
    journal_sequences: dict[str, int] = {}
    runtime_incomplete = journal_incomplete = False
    try:
        for account in accounts:
            relative = account.relative_to(root).as_posix()
            checkpoint_path = account / "autonomous-runtime-checkpoint.json"
            journal_path = account / "journal.sqlite3"
            if not checkpoint_path.is_file():
                runtime_incomplete = True
            else:
                checkpoint = _load_json(checkpoint_path, root)
                replay = checkpoint.get("replay")
                components = checkpoint.get("runtime_components")
                if (checkpoint.get("schema_version") != "4.0.0"
                        or checkpoint.get("build_sha") != expected_build_sha256
                        or type(replay) is not dict
                        or not _is_digest(replay.get("dataset_digest"))
                        or type(replay.get("cursor")) is not int or replay["cursor"] < 0
                        or type(components) is not dict or not components
                        or any(not _is_digest(item) for item in components.values())):
                    raise QualificationError("runtime checkpoint identity or components differ")
                checkpoint_hashes[relative] = _sha256_file(checkpoint_path)
            if not journal_path.is_file():
                journal_incomplete = True
                continue
            sequence, event_types = _read_journal(journal_path, root)
            journal_sequences[relative] = sequence
            required_groups = (
                {"SimulationAgentDecisionRecorded"},
                {"RiskDecisionRecorded"},
                {"AutonomousEpisodeFillObserved"},
                {"AutonomousEpisodeCompleted", "AccountReconciled"},
            )
            if any(not (group & event_types) for group in required_groups):
                journal_incomplete = True
            if any("Live" in name or "RealOrder" in name for name in event_types):
                raise QualificationError("journal contains a real-route event type")
    except (OSError, KeyError, QualificationError, FileNotFoundError) as exc:
        _check(checks, "durable_runtime", "FAIL", str(exc))
        _check(checks, "financial_journals", "FAIL",
               "durable financial evidence cannot be trusted")
        return accounts, {}, {}
    _check(checks, "durable_runtime", "INCOMPLETE" if runtime_incomplete else "PASS",
           "one or more runtime checkpoints are missing" if runtime_incomplete else
           "all account checkpoints bind the requested build and replay state")
    _check(checks, "financial_journals", "INCOMPLETE" if journal_incomplete else "PASS",
           "agent/risk/execution/accounting events are not all present" if journal_incomplete else
           "durable journals contain contiguous agent, risk, execution and accounting evidence")
    return accounts, checkpoint_hashes, journal_sequences


def _restart_evidence(root: Path, source_sha: str, build_sha256: str,
                      accounts: list[Path], checkpoint_hashes: dict[str, str],
                      journal_sequences: dict[str, int], checks: list[dict[str, str]]) -> None:
    path = root / "qualification" / "restart-evidence.json"
    if not path.exists():
        _check(checks, "restart_recovery", "INCOMPLETE",
               "explicit restart/recovery evidence is missing")
        return
    try:
        value = _load_json(path, root)
        rows = value.get("accounts")
        if (value.get("schema") != RESTART_SCHEMA or value.get("status") != "PASS"
                or value.get("source_sha") != source_sha
                or value.get("build_sha256") != build_sha256 or type(rows) is not dict):
            raise QualificationError("restart evidence identity or status differs")
        expected = {path.relative_to(root).as_posix() for path in accounts}
        if set(rows) != expected or set(checkpoint_hashes) != expected or set(journal_sequences) != expected:
            raise QualificationError("restart evidence does not cover every account")
        for relative, row in rows.items():
            if (type(row) is not dict or row.get("recovered_without_duplicate_effects") is not True
                    or not _is_digest(row.get("checkpoint_sha256_before"))
                    or row.get("checkpoint_sha256_after") != checkpoint_hashes[relative]
                    or type(row.get("journal_sequence_before")) is not int
                    or type(row.get("journal_sequence_after")) is not int
                    or not 1 <= row["journal_sequence_before"] <= row["journal_sequence_after"]
                    or row["journal_sequence_after"] != journal_sequences[relative]):
                raise QualificationError("restart evidence is incomplete or stale")
    except (OSError, KeyError, QualificationError, FileNotFoundError) as exc:
        _check(checks, "restart_recovery", "FAIL", str(exc))
        return
    _check(checks, "restart_recovery", "PASS",
           "every account has current checkpoint-bound restart evidence")


def _safe_zip_members(path: Path, root: Path) -> tuple[dict[str, bytes], dict[str, Any]]:
    _reject_alias(path, root)
    if not path.is_file() or path.stat().st_size > MAX_DIAGNOSTIC_ZIP_BYTES:
        raise QualificationError("diagnostic archive is not a bounded ordinary file")
    members: dict[str, bytes] = {}
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 256:
                raise QualificationError("diagnostic archive member count is invalid")
            total = 0
            for info in infos:
                member = Path(info.filename)
                if (member.is_absolute() or ".." in member.parts or info.flag_bits & 1
                        or info.file_size > MAX_JSON_BYTES):
                    raise QualificationError("diagnostic archive has an unsafe member")
                total += info.file_size
                if total > MAX_DIAGNOSTIC_ZIP_BYTES:
                    raise QualificationError("diagnostic archive expands beyond its bound")
                members[info.filename] = archive.read(info)
    except BadZipFile as exc:
        raise QualificationError("diagnostic archive is malformed") from exc
    try:
        manifest = json.loads(
            members["manifest.json"], object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                QualificationError(f"non-finite diagnostic value: {value}")),
        )
    except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError("diagnostic archive manifest is malformed") from exc
    if type(manifest) is not dict or type(manifest.get("members")) is not dict:
        raise QualificationError("diagnostic archive manifest schema differs")
    if set(members) - {"manifest.json"} != set(manifest["members"]):
        raise QualificationError("diagnostic archive has an unbound or missing member")
    for name, identity in manifest["members"].items():
        body = members.get(name)
        if (body is None or type(identity) is not dict
                or identity.get("bytes") != len(body)
                or identity.get("sha256") != sha256(body).hexdigest()):
            raise QualificationError("diagnostic member digest differs")
    return members, manifest


def _diagnostics(root: Path, source_sha: str, checks: list[dict[str, str]]) -> list[tuple[str, bytes]]:
    diagnostics = root / "diagnostics"
    agent_paths = sorted(diagnostics.rglob("autotrade-agent-report*.zip")) if diagnostics.is_dir() else []
    technical_paths = (sorted(diagnostics.rglob("autotrade-technical-diagnostics*.zip"))
                       if diagnostics.is_dir() else [])
    if not agent_paths or not technical_paths:
        _check(checks, "dual_diagnostics", "INCOMPLETE",
               "separate agent report and technical diagnostics archives are required")
        return []
    scanned: list[tuple[str, bytes]] = []
    try:
        for kind, paths in (("AGENT_REPORT", agent_paths),
                            ("TECHNICAL_DIAGNOSTICS", technical_paths)):
            for path in paths:
                members, manifest = _safe_zip_members(path, root)
                if (manifest.get("report_kind") != kind
                        or manifest.get("source_revision") != source_sha
                        or manifest.get("simulation_only") is not True):
                    raise QualificationError("diagnostic archive identity or scope differs")
                if kind == "AGENT_REPORT" and (
                        manifest.get("complete_agent_evidence") is not True
                        or type(manifest.get("agent_action_count")) is not int
                        or manifest["agent_action_count"] < 1):
                    raise QualificationError("agent diagnostic evidence is incomplete")
                if kind == "TECHNICAL_DIAGNOSTICS" and (
                        manifest.get("financial_authority") is not False
                        or type(manifest.get("sample_count")) is not int
                        or manifest["sample_count"] < 1
                        or manifest.get("retention", {}).get("persistent_across_restart") is not True):
                    raise QualificationError("technical diagnostic evidence is incomplete")
                scanned.extend((path.relative_to(root).as_posix() + ":" + name, body)
                               for name, body in members.items()
                               if name.endswith((".json", ".jsonl", ".csv", ".txt")))
    except (OSError, QualificationError, FileNotFoundError) as exc:
        _check(checks, "dual_diagnostics", "FAIL", str(exc))
        return scanned
    _check(checks, "dual_diagnostics", "PASS",
           "agent evidence and technical telemetry are separate and integrity-bound")
    return scanned


def _contains_secret(body: bytes) -> bool:
    if b"\x00" in body:
        return False
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        return False
    return any(pattern.search(text) is not None for pattern in _SECRET_PATTERNS)


def _secret_scan(root: Path, archive_members: list[tuple[str, bytes]],
                 checks: list[dict[str, str]]) -> None:
    suffixes = {".json", ".jsonl", ".log", ".txt", ".csv"}
    findings: list[str] = []
    scanned = 0
    try:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            scanned += 1
            if scanned > MAX_SCANNED_FILES:
                raise QualificationError("public artifact scan exceeds its file bound")
            _reject_alias(path, root)
            if path.stat().st_size > MAX_JSON_BYTES:
                raise QualificationError("public text artifact exceeds its scan bound")
            if _contains_secret(path.read_bytes()):
                findings.append(path.relative_to(root).as_posix())
        for name, body in archive_members:
            if _contains_secret(body):
                findings.append(name)
    except (OSError, QualificationError, FileNotFoundError) as exc:
        _check(checks, "secret_leakage", "FAIL", str(exc))
        return
    if findings:
        _check(checks, "secret_leakage", "FAIL",
               "credential-like material appears in public artifact(s): " + ", ".join(findings[:5]))
    else:
        _check(checks, "secret_leakage", "PASS",
               "bounded public artifacts contain no recognized credential material")


def qualify_workspace(workspace: Path, *, source_sha: str,
                      build_sha256: str) -> dict[str, Any]:
    """Return a deterministic PASS/FAIL/INCOMPLETE workspace assessment."""
    if type(workspace) is not type(Path()):
        raise TypeError("workspace must be an exact Path")
    if not _is_source_sha(source_sha) or not _is_digest(build_sha256):
        raise ValueError("source/build identity must be lowercase hexadecimal")
    workspace = workspace.absolute()
    if not workspace.is_dir() or workspace.is_symlink():
        raise ValueError("historical workspace must be an ordinary directory")
    checks: list[dict[str, str]] = []
    _identity(workspace, source_sha, build_sha256, checks)
    models, validations = _learning_artifacts(workspace, checks)
    reports = _campaign_reports(workspace, models, validations, checks)
    accounts, checkpoint_hashes, journal_sequences = _runtime_evidence(
        workspace, build_sha256, checks)
    _restart_evidence(workspace, source_sha, build_sha256, accounts,
                      checkpoint_hashes, journal_sequences, checks)
    archive_members = _diagnostics(workspace, source_sha, checks)
    _secret_scan(workspace, archive_members, checks)
    statuses = {row["status"] for row in checks}
    status = "FAIL" if "FAIL" in statuses else "INCOMPLETE" if "INCOMPLETE" in statuses else "PASS"
    return {
        "schema": SCHEMA,
        "status": status,
        "workspace": str(workspace),
        "source_sha": source_sha,
        "build_sha256": build_sha256,
        "campaign_report_count": len(reports),
        "account_workspace_count": len(accounts),
        "checks": checks,
        "claim_scope": "INTERNAL_ZERO_HISTORICAL_SIMULATION_ONLY",
        "limitations": [
            "No provider account, PAPER/LIVE route, real fill, revenue or profitability is claimed.",
            "INCOMPLETE means evidence is absent or a campaign is still in progress; it is never a PASS.",
        ],
    }


def _publish(path: Path, payload: dict[str, Any]) -> None:
    destination = path.absolute()
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise ValueError("output must be a new JSON file in an existing directory")
    body = (json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".historical-e2e-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Qualify an existing Plan10 historical workspace without running it")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-sha256", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        report = qualify_workspace(Path(args.workspace), source_sha=args.source_sha,
                                   build_sha256=args.build_sha256)
        if args.output:
            _publish(Path(args.output), report)
        print(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "PASS" else 2
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        print(f"AutoTrade historical E2E qualification blocked: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
