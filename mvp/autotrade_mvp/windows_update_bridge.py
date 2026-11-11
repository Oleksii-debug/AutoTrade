"""Executable, fail-closed bridge for qualified Windows updates.

The release planner in :mod:`windows_update` deliberately has no process or
filesystem authority.  This module is the small production boundary that turns
one already-qualified plan into a side-by-side install.  It never replaces a
running program: the runtime controller must first return a checkpointed STOP
receipt and this process must independently acquire every canonical Host and
public-learning lock.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
import zipfile

from research.autotrade_research.artifacts.resource_lock import (
    ResourceLock,
    ResourceLockBusyError,
)
from research.autotrade_research.artifacts.store import ArtifactStore

from .backup import (
    MANIFEST_DIGEST_NAME,
    create_backup,
    verify_backup,
)
from .release_candidate import ReleaseCandidateDecision, ReleaseCandidateError
from .windows_update import (
    BackupEvidence,
    MigrationEvidence,
    WindowsUpdateCheckpoint,
    WindowsUpdateError,
    WindowsUpdatePlan,
    WindowsUpdateTrustContext,
    advance_rollback_checkpoint,
    advance_update_checkpoint,
    build_windows_update_plan,
    serialize_update_checkpoint,
    start_rollback_checkpoint,
    start_update_checkpoint,
)


class WindowsUpdateBridgeError(RuntimeError):
    """The executable update bridge could not prove a safe next action."""


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^autotrade-(\d+)\.(\d+)\.(\d+)(?:[-+][a-z0-9.-]+)?$")
_MAX_DESCRIPTOR = 1024 * 1024
_MAX_RELEASE_MANIFEST = 16 * 1024 * 1024
_MAX_PACKAGE = 8 * 1024 * 1024 * 1024
_MAX_ARCHIVE_FILES = 50_000
_MAX_EXTRACTED_BYTES = 16 * 1024 * 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True)
class WindowsUpdateBridgeRequest:
    descriptor_url: str
    descriptor_sha256: str
    current_release_manifest: Path
    evidence_root: Path
    state_dir: Path
    artifact_root: Path
    data_root: Path
    install_root: Path
    backup_root: Path
    work_root: Path
    controller_executable: Path

    def __post_init__(self) -> None:
        _https_url(self.descriptor_url, name="descriptor_url")
        _digest(self.descriptor_sha256, name="descriptor_sha256")
        for name in (
            "current_release_manifest",
            "evidence_root",
            "state_dir",
            "artifact_root",
            "data_root",
            "install_root",
            "backup_root",
            "work_root",
            "controller_executable",
        ):
            value = getattr(self, name)
            if type(value) is not type(Path()) or not value.is_absolute():
                raise WindowsUpdateBridgeError(f"{name} must be an absolute Path")
        data = self.data_root.resolve(strict=False)
        install = self.install_root.resolve(strict=False)
        if data == install or data in install.parents or install in data.parents:
            raise WindowsUpdateBridgeError(
                "durable data and the replaceable install root must be separate"
            )


class SubprocessUpdateController:
    """Strict request/response adapter for the installed Windows service."""

    def __init__(self, executable: Path, *, timeout_seconds: int = 120) -> None:
        self.executable = _ordinary_absolute_file(executable, name="controller")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 900:
            raise WindowsUpdateBridgeError("controller timeout is invalid")
        self.timeout_seconds = timeout_seconds

    def request(self, action: str, payload: dict[str, Any], *, directory: Path) -> dict[str, Any]:
        if action not in {
            "prepare-update",
            "apply-migration",
            "resume-degraded",
            "prepare-rollback",
            "restore-backup",
            "resume-rollback-degraded",
        }:
            raise WindowsUpdateBridgeError("unsupported controller action")
        directory.mkdir(parents=True, exist_ok=True)
        nonce = uuid4().hex
        request_path = directory / f"controller-{nonce}.request.json"
        response_path = directory / f"controller-{nonce}.response.json"
        _atomic_json(request_path, payload)
        try:
            completed = subprocess.run(
                [
                    str(self.executable),
                    "--autotrade-update-action",
                    action,
                    "--request",
                    str(request_path),
                    "--response",
                    str(response_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=False,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                raise WindowsUpdateBridgeError("runtime controller rejected update action")
            return _read_json_file(response_path, maximum=_MAX_DESCRIPTOR)
        except subprocess.TimeoutExpired as error:
            raise WindowsUpdateBridgeError("runtime controller timed out") from error
        finally:
            request_path.unlink(missing_ok=True)
            response_path.unlink(missing_ok=True)


def _digest(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise WindowsUpdateBridgeError(f"{name} must be canonical sha256")
    return value


def _https_url(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise WindowsUpdateBridgeError(f"{name} must be HTTPS")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise WindowsUpdateBridgeError(f"{name} must be an HTTPS URL without credentials")
    return value


def _ordinary_absolute_file(path: Path, *, name: str) -> Path:
    if type(path) is not type(Path()) or not path.is_absolute():
        raise WindowsUpdateBridgeError(f"{name} must be an absolute Path")
    try:
        info = path.lstat()
    except OSError as error:
        raise WindowsUpdateBridgeError(f"{name} is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)
    ):
        raise WindowsUpdateBridgeError(f"{name} must be an ordinary file")
    return path


def _read_json_file(path: Path, *, maximum: int) -> dict[str, Any]:
    path = _ordinary_absolute_file(path, name="JSON input")
    try:
        if path.stat().st_size > maximum:
            raise WindowsUpdateBridgeError("JSON input exceeds its size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WindowsUpdateBridgeError("JSON input is unreadable") from error
    if type(value) is not dict:
        raise WindowsUpdateBridgeError("JSON input must be an object")
    return value


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    payload = _canonical_bytes(value)
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _download(url: str, destination: Path, *, expected_sha256: str, maximum: int) -> Path:
    _https_url(url, name="download URL")
    expected = _digest(expected_sha256, name="download digest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = sha256()
    observed = 0
    request = Request(url, headers={"User-Agent": "AutoTrade-Updater/1"})
    try:
        with urlopen(request, timeout=60) as response, destination.open("xb") as stream:
            _https_url(response.geturl(), name="final download URL")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > maximum:
                    raise WindowsUpdateBridgeError("download exceeds its size limit")
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    except WindowsUpdateBridgeError:
        destination.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as error:
        destination.unlink(missing_ok=True)
        raise WindowsUpdateBridgeError("download failed") from error
    actual = "sha256:" + digest.hexdigest()
    if observed == 0 or actual != expected:
        destination.unlink(missing_ok=True)
        raise WindowsUpdateBridgeError("download digest does not match manifest")
    return destination


def _release_decision(path: Path, expected_sha256: str, trust: WindowsUpdateTrustContext) -> ReleaseCandidateDecision:
    raw = _ordinary_absolute_file(path, name="release manifest").read_bytes()
    digest = "sha256:" + sha256(raw).hexdigest()
    if digest != _digest(expected_sha256, name="release manifest digest"):
        raise WindowsUpdateBridgeError("release manifest digest mismatch")
    try:
        manifest_json = raw.decode("utf-8")
        value = json.loads(manifest_json)
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        qualification = value["qualification"]
        decision = ReleaseCandidateDecision(
            status="FROZEN",
            reasons=(),
            manifest_json=canonical,
            manifest_sha256="sha256:" + sha256(canonical.encode("utf-8")).hexdigest(),
            qualification_attestation_id=qualification["attestation_id"],
            qualification_attestation_digest=qualification["attestation_digest"],
            qualification_policy_id=qualification["policy_id"],
            qualification_trust_root_id=qualification["trust_root_id"],
            verification_store=trust.evidence_store,
            verification_root=trust.evidence_root,
        )
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError, ReleaseCandidateError) as error:
        raise WindowsUpdateBridgeError("release manifest is not a trusted frozen release") from error
    return decision


def _release_version(manifest_json: str, *, expected: str) -> tuple[int, int, int]:
    try:
        release_id = json.loads(manifest_json)["release_id"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise WindowsUpdateBridgeError("release identity is unavailable") from error
    if release_id != expected:
        raise WindowsUpdateBridgeError("release identity does not match descriptor")
    match = _RELEASE_ID.fullmatch(expected)
    if match is None:
        raise WindowsUpdateBridgeError("release identity is not a supported semantic version")
    return tuple(int(item) for item in match.groups())


def _validate_descriptor(value: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version",
        "current_release_id",
        "candidate_release_id",
        "candidate_manifest_url",
        "candidate_manifest_sha256",
        "candidate_package_url",
        "candidate_package_sha256",
        "current_journal_schema_version",
        "candidate_journal_schema_version",
        "migration_evidence",
    }
    if set(value) != expected or value.get("schema_version") != "1.0.0":
        raise WindowsUpdateBridgeError("update descriptor structure is unsupported")
    _https_url(value["candidate_manifest_url"], name="candidate_manifest_url")
    _https_url(value["candidate_package_url"], name="candidate_package_url")
    _digest(value["candidate_manifest_sha256"], name="candidate_manifest_sha256")
    _digest(value["candidate_package_sha256"], name="candidate_package_sha256")
    for name in ("current_journal_schema_version", "candidate_journal_schema_version"):
        if type(value[name]) is not int or value[name] <= 0:
            raise WindowsUpdateBridgeError(f"{name} must be a positive integer")
    if type(value["current_release_id"]) is not str or type(value["candidate_release_id"]) is not str:
        raise WindowsUpdateBridgeError("release identities are required")
    return value


def _migration_evidence(raw: object) -> MigrationEvidence | None:
    if raw is None:
        return None
    if type(raw) is not dict or set(raw) != {
        "from_schema_version", "to_schema_version", "source_sha",
        "evidence_sha256", "verification_status", "rollback_mode",
        "reverse_evidence_sha256",
    }:
        raise WindowsUpdateBridgeError("migration evidence is not canonical")
    try:
        return MigrationEvidence(**raw)
    except (TypeError, ValueError, WindowsUpdateError) as error:
        raise WindowsUpdateBridgeError("migration evidence is invalid") from error


def _assert_prepare_receipt(receipt: dict[str, Any], *, request_id: str, action: str) -> None:
    expected = {
        "schema_version", "request_id", "action", "status",
        "financial_process_running", "financial_sender_fenced",
        "checkpoint_confirmed", "learning_checkpoint_confirmed",
        "trading_authority",
    }
    if set(receipt) != expected or receipt.get("schema_version") != "1.0.0":
        raise WindowsUpdateBridgeError("runtime STOP receipt is malformed")
    if (
        receipt.get("request_id") != request_id
        or receipt.get("action") != action
        or receipt.get("status") != "STOPPED_CHECKPOINTED"
        or receipt.get("financial_process_running") is not False
        or receipt.get("financial_sender_fenced") is not True
        or receipt.get("checkpoint_confirmed") is not True
        or receipt.get("learning_checkpoint_confirmed") is not True
        or receipt.get("trading_authority") is not False
    ):
        raise WindowsUpdateBridgeError("runtime did not reach checkpointed STOP")


def _assert_resume_receipt(receipt: dict[str, Any], *, request_id: str, action: str) -> None:
    expected = {
        "schema_version", "request_id", "action", "status",
        "financial_sender_fenced", "checkpoint_confirmed",
        "learning_checkpoint_confirmed", "learning_resumed", "identity_validated",
        "provider_sessions_reestablished", "reconciliation_completed",
        "host_ui_compatible", "trading_authority",
    }
    if set(receipt) != expected or receipt.get("schema_version") != "1.0.0":
        raise WindowsUpdateBridgeError("runtime resume receipt is malformed")
    if (
        receipt.get("request_id") != request_id
        or receipt.get("action") != action
        or receipt.get("status") != "DEGRADED_RESEARCH_ONLY"
        or any(receipt.get(name) is not True for name in (
            "financial_sender_fenced", "checkpoint_confirmed",
            "learning_checkpoint_confirmed", "learning_resumed", "identity_validated",
            "provider_sessions_reestablished", "reconciliation_completed",
            "host_ui_compatible",
        ))
        or receipt.get("trading_authority") is not False
    ):
        raise WindowsUpdateBridgeError("runtime did not resume safely in degraded mode")


def _runtime_locks(request: WindowsUpdateBridgeRequest) -> ExitStack:
    paths = (
        request.state_dir / "journal.sqlite3.host.lock",
        request.data_root / "owned-numerical-research.lock",
        request.data_root / "whitebit-public-supervisor" / "whitebit-public-supervisor.lock",
        request.data_root / "whitebit-live-zero-learning" / "whitebit-live-zero-learning.lock",
    )
    stack = ExitStack()
    try:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            stack.enter_context(ResourceLock(path, blocking=False))
    except ResourceLockBusyError as error:
        stack.close()
        raise WindowsUpdateBridgeError(
            "a financial or learning process is still running; update refused"
        ) from error
    return stack


def _safe_extract(package: Path, install_root: Path, release_id: str, source_sha: str) -> Path:
    releases = install_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    destination = releases / f"{release_id}-{source_sha[:12]}"
    if destination.exists():
        raise WindowsUpdateBridgeError("candidate release directory already exists")
    stage = Path(tempfile.mkdtemp(prefix=".autotrade-install-", dir=releases))
    names: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(package) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > _MAX_ARCHIVE_FILES:
                raise WindowsUpdateBridgeError("candidate archive inventory is invalid")
            for info in infos:
                if info.flag_bits & 0x1 or "\\" in info.filename or "\x00" in info.filename:
                    raise WindowsUpdateBridgeError("candidate archive contains unsafe paths")
                relative = PurePosixPath(info.filename)
                if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                    raise WindowsUpdateBridgeError("candidate archive contains unsafe paths")
                key = "/".join(relative.parts).casefold()
                if key in names:
                    raise WindowsUpdateBridgeError("candidate archive contains duplicate paths")
                names.add(key)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise WindowsUpdateBridgeError("candidate archive contains a link")
                total += info.file_size
                if total > _MAX_EXTRACTED_BYTES:
                    raise WindowsUpdateBridgeError("candidate archive expands beyond its limit")
                target = stage.joinpath(*relative.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
        os.replace(stage, destination)
        return destination
    except (OSError, zipfile.BadZipFile) as error:
        raise WindowsUpdateBridgeError("candidate package is not an installable ZIP") from error
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def _checkpoint_path(request: WindowsUpdateBridgeRequest) -> Path:
    return request.work_root / "update-checkpoint.json"


def _persist_checkpoint(request: WindowsUpdateBridgeRequest, checkpoint: WindowsUpdateCheckpoint) -> None:
    _atomic_json(
        _checkpoint_path(request),
        {"schema_version": "1.0.0", "checkpoint_json": serialize_update_checkpoint(checkpoint)},
    )


def _activation(request: WindowsUpdateBridgeRequest) -> tuple[dict[str, Any] | None, Path]:
    path = request.install_root / "active-release.json"
    if not path.exists():
        return None, path
    value = _read_json_file(path, maximum=_MAX_DESCRIPTOR)
    if set(value) != {
        "schema_version", "release_id", "source_sha", "package_sha256",
        "path", "plan_sha256",
    } or value.get("schema_version") != "1.0.0":
        raise WindowsUpdateBridgeError("active release pointer is malformed")
    if (
        type(value.get("release_id")) is not str
        or _RELEASE_ID.fullmatch(value["release_id"]) is None
        or type(value.get("source_sha")) is not str
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value["source_sha"])
        is None
    ):
        raise WindowsUpdateBridgeError("active release identity is malformed")
    _digest(value.get("package_sha256"), name="active package digest")
    _digest(value.get("plan_sha256"), name="active plan digest")
    if type(value.get("path")) is not str:
        raise WindowsUpdateBridgeError("active release path is malformed")
    active = Path(value["path"])
    releases = (request.install_root / "releases").resolve(strict=False)
    try:
        resolved = active.resolve(strict=True)
    except OSError as error:
        raise WindowsUpdateBridgeError("active release path is unavailable") from error
    if resolved.parent != releases or not resolved.is_dir() or active.is_symlink():
        raise WindowsUpdateBridgeError("active release escapes the versioned install root")
    return value, path


def _advance(
    plan: WindowsUpdatePlan,
    checkpoint: WindowsUpdateCheckpoint,
    step: str,
    *,
    trust: WindowsUpdateTrustContext,
    request: WindowsUpdateBridgeRequest,
    package: Path | None = None,
) -> WindowsUpdateCheckpoint:
    checkpoint = advance_update_checkpoint(
        plan, checkpoint, step, trust=trust, payload_path=package
    )
    _persist_checkpoint(request, checkpoint)
    return checkpoint


def run_windows_update(
    request: WindowsUpdateBridgeRequest,
    *,
    controller: SubprocessUpdateController | None = None,
) -> dict[str, Any]:
    """Download, verify and activate one release without restoring trade authority."""

    if not isinstance(request, WindowsUpdateBridgeRequest):
        raise TypeError("request must be WindowsUpdateBridgeRequest")
    request.work_root.mkdir(parents=True, exist_ok=True)
    request.backup_root.mkdir(parents=True, exist_ok=True)
    request.install_root.mkdir(parents=True, exist_ok=True)
    controller = controller or SubprocessUpdateController(request.controller_executable)
    bridge_lock = ResourceLock(
        request.work_root / "windows-update-bridge.lock", blocking=False
    )
    try:
        bridge_lock.acquire()
    except ResourceLockBusyError as error:
        raise WindowsUpdateBridgeError(
            "another Windows updater already owns this installation"
        ) from error
    request_id = uuid4().hex
    try:
        stage = Path(
            tempfile.mkdtemp(prefix="autotrade-update-", dir=request.work_root)
        )
    except BaseException:
        bridge_lock.release()
        raise
    plan: WindowsUpdatePlan | None = None
    checkpoint: WindowsUpdateCheckpoint | None = None
    previous_activation: dict[str, Any] | None = None
    activation_path: Path | None = None
    side_effect_started = False
    try:
        descriptor_path = _download(
            request.descriptor_url,
            stage / "update-descriptor.json",
            expected_sha256=request.descriptor_sha256,
            maximum=_MAX_DESCRIPTOR,
        )
        descriptor = _validate_descriptor(_read_json_file(descriptor_path, maximum=_MAX_DESCRIPTOR))
        candidate_manifest = _download(
            descriptor["candidate_manifest_url"],
            stage / "candidate-release.json",
            expected_sha256=descriptor["candidate_manifest_sha256"],
            maximum=_MAX_RELEASE_MANIFEST,
        )
        package = _download(
            descriptor["candidate_package_url"],
            stage / "candidate-package.zip",
            expected_sha256=descriptor["candidate_package_sha256"],
            maximum=_MAX_PACKAGE,
        )
        store = ArtifactStore(request.evidence_root)
        trust = WindowsUpdateTrustContext(evidence_store=store, evidence_root=request.evidence_root)
        current_raw = _ordinary_absolute_file(request.current_release_manifest, name="current release manifest").read_bytes()
        current_digest = "sha256:" + sha256(current_raw).hexdigest()
        current = _release_decision(request.current_release_manifest, current_digest, trust)
        candidate = _release_decision(candidate_manifest, descriptor["candidate_manifest_sha256"], trust)
        assert current.manifest_json is not None and candidate.manifest_json is not None
        current_version = _release_version(current.manifest_json, expected=descriptor["current_release_id"])
        candidate_version = _release_version(candidate.manifest_json, expected=descriptor["candidate_release_id"])
        if candidate_version <= current_version:
            raise WindowsUpdateBridgeError("candidate version must be newer than installed version")

        stop_payload = {
            "schema_version": "1.0.0",
            "request_id": request_id,
            "candidate_manifest_sha256": descriptor["candidate_manifest_sha256"],
        }
        stop_receipt = controller.request("prepare-update", stop_payload, directory=stage)
        _assert_prepare_receipt(stop_receipt, request_id=request_id, action="prepare-update")

        with _runtime_locks(request):
            backup_destination = request.backup_root / f"pre-update-{request_id}"
            create_backup(request.state_dir, request.artifact_root, backup_destination)
            backup_manifest = verify_backup(backup_destination)
            backup_digest = (backup_destination / MANIFEST_DIGEST_NAME).read_text(encoding="ascii").strip()
            migration = _migration_evidence(descriptor["migration_evidence"])
            current_source = json.loads(current.manifest_json)["source_sha"]
            backup = BackupEvidence(
                manifest_sha256=backup_digest,
                source_sha=current_source,
                journal_schema_version=backup_manifest["journal_schema_version"],
                verification_status="PASS",
                reconciliation_required_after_restore=backup_manifest["reconciliation_required_after_restore"],
            )
            plan = build_windows_update_plan(
                current_release=current,
                candidate_release=candidate,
                current_journal_schema_version=descriptor["current_journal_schema_version"],
                candidate_journal_schema_version=descriptor["candidate_journal_schema_version"],
                backup_evidence=backup,
                trust=trust,
                migration_evidence=migration,
            )
            if plan.status != "PLAN_READY":
                raise WindowsUpdateBridgeError("qualified update plan is blocked")
            checkpoint = start_update_checkpoint(plan, trust=trust)
            _persist_checkpoint(request, checkpoint)
            checkpoint = _advance(plan, checkpoint, "VERIFY_CANDIDATE_SIGNATURE_AND_EXACT_HASH", trust=trust, request=request, package=package)
            for step in (
                "QUIESCE_NEW_ADMISSIONS",
                "SURFACE_AND_RECONCILE_IN_FLIGHT_PROVIDER_SENDS",
                "VERIFY_PRE_UPDATE_BACKUP",
                "STOP_AND_FENCE_FINANCIAL_SENDER",
            ):
                checkpoint = _advance(plan, checkpoint, step, trust=trust, request=request)
            candidate_source = json.loads(candidate.manifest_json)["source_sha"]
            previous_activation, activation_path = _activation(request)
            installed = _safe_extract(package, request.install_root, descriptor["candidate_release_id"], candidate_source)
            side_effect_started = True
            checkpoint = _advance(plan, checkpoint, "INSTALL_CANDIDATE_SIDE_BY_SIDE", trust=trust, request=request, package=package)
            if migration is not None:
                migration_receipt = controller.request(
                    "apply-migration",
                    {
                        "schema_version": "1.0.0",
                        "request_id": request_id,
                        "plan_sha256": plan.plan_sha256,
                        "evidence_sha256": migration.evidence_sha256,
                        "backup_path": str(backup_destination),
                    },
                    directory=stage,
                )
                if migration_receipt != {
                    "schema_version": "1.0.0",
                    "request_id": request_id,
                    "action": "apply-migration",
                    "status": "MIGRATION_APPLIED",
                    "evidence_sha256": migration.evidence_sha256,
                    "trading_authority": False,
                }:
                    raise WindowsUpdateBridgeError("verified migration was not applied")
            checkpoint = _advance(plan, checkpoint, "APPLY_VERIFIED_SCHEMA_TRANSITION_IF_REQUIRED", trust=trust, request=request)
            _atomic_json(
                activation_path,
                {
                    "schema_version": "1.0.0",
                    "release_id": descriptor["candidate_release_id"],
                    "source_sha": candidate_source,
                    "package_sha256": descriptor["candidate_package_sha256"],
                    "path": str(installed),
                    "plan_sha256": plan.plan_sha256,
                },
            )

        resume = controller.request(
            "resume-degraded",
            {
                "schema_version": "1.0.0",
                "request_id": request_id,
                "plan_sha256": plan.plan_sha256,
                "active_release": str(activation_path),
            },
            directory=stage,
        )
        _assert_resume_receipt(resume, request_id=request_id, action="resume-degraded")
        for step in (
            "START_DEGRADED_NO_TRADING_AUTHORITY",
            "VALIDATE_JOURNAL_STORAGE_CLOCK_SECURITY_IDENTITY",
            "REESTABLISH_AUTHENTICATED_PROVIDER_SESSIONS",
            "RUN_POST_UPDATE_RECONCILIATION",
            "VERIFY_HOST_UI_COMPATIBILITY",
            "ENTER_READY_FOR_SEPARATE_AUTHORITY_REACQUISITION",
        ):
            checkpoint = _advance(plan, checkpoint, step, trust=trust, request=request)
        result = {
            "schema_version": "1.0.0",
            "status": "UPDATED_DEGRADED_RESEARCH_ONLY",
            "request_id": request_id,
            "release_id": descriptor["candidate_release_id"],
            "plan_sha256": plan.plan_sha256,
            "backup_path": str(backup_destination),
            "active_release": str(activation_path),
            "learning_resumed": True,
            "trading_authority": False,
        }
        _atomic_json(request.work_root / "last-update-result.json", result)
        return result
    except BaseException as failure:
        if side_effect_started and plan is not None and checkpoint is not None and activation_path is not None:
            try:
                checkpoint = start_rollback_checkpoint(plan, checkpoint, trust=trust)
                _persist_checkpoint(request, checkpoint)
                rollback_stop = controller.request(
                    "prepare-rollback",
                    {"schema_version": "1.0.0", "request_id": request_id, "plan_sha256": plan.plan_sha256},
                    directory=stage,
                )
                _assert_prepare_receipt(rollback_stop, request_id=request_id, action="prepare-rollback")
                with _runtime_locks(request):
                    for step in (
                        "QUIESCE_NEW_ADMISSIONS",
                        "SURFACE_AND_RECONCILE_IN_FLIGHT_PROVIDER_SENDS",
                        "STOP_AND_FENCE_FINANCIAL_SENDER",
                    ):
                        checkpoint = advance_rollback_checkpoint(plan, checkpoint, step, trust=trust)
                        _persist_checkpoint(request, checkpoint)
                    restore = controller.request(
                        "restore-backup",
                        {
                            "schema_version": "1.0.0",
                            "request_id": request_id,
                            "plan_sha256": plan.plan_sha256,
                            "backup_path": str(backup_destination),
                        },
                        directory=stage,
                    )
                    if restore != {
                        "schema_version": "1.0.0", "request_id": request_id,
                        "action": "restore-backup", "status": "BACKUP_RESTORED",
                        "reconciliation_required": True, "trading_authority": False,
                    }:
                        raise WindowsUpdateBridgeError("rollback backup restore is unconfirmed")
                    if previous_activation is None:
                        activation_path.unlink(missing_ok=True)
                    else:
                        _atomic_json(activation_path, previous_activation)
                    checkpoint = advance_rollback_checkpoint(
                        plan, checkpoint, "RESTORE_OR_REINSTALL_PREVIOUS_QUALIFIED_STATE", trust=trust
                    )
                    _persist_checkpoint(request, checkpoint)
                rollback_resume = controller.request(
                    "resume-rollback-degraded",
                    {"schema_version": "1.0.0", "request_id": request_id, "plan_sha256": plan.plan_sha256},
                    directory=stage,
                )
                _assert_resume_receipt(rollback_resume, request_id=request_id, action="resume-rollback-degraded")
                for step in (
                    "START_DEGRADED_NO_TRADING_AUTHORITY",
                    "VALIDATE_JOURNAL_STORAGE_CLOCK_SECURITY_IDENTITY",
                    "REESTABLISH_AUTHENTICATED_PROVIDER_SESSIONS",
                    "RUN_POST_RESTORE_RECONCILIATION",
                    "VERIFY_HOST_UI_COMPATIBILITY",
                    "ENTER_READY_FOR_SEPARATE_AUTHORITY_REACQUISITION",
                ):
                    checkpoint = advance_rollback_checkpoint(plan, checkpoint, step, trust=trust)
                    _persist_checkpoint(request, checkpoint)
            except BaseException as rollback_failure:
                raise WindowsUpdateBridgeError(
                    "update failed and rollback remains checkpointed for RECOVERY"
                ) from rollback_failure
        if isinstance(failure, WindowsUpdateBridgeError):
            raise
        raise WindowsUpdateBridgeError("Windows update failed closed") from failure
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        bridge_lock.release()
