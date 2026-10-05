"""Bounded raw process/disk evidence for the current WP-65 target-host runner.

This composes the existing current-schema runner. It creates no scheduler,
journal, evaluator, host, chronology, provider or trading authority.
``COLLECTED_PROCESS_DISK_V1`` is nonterminal raw evidence only.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from hashlib import sha256
from ctypes import (
    Structure as _Structure,
    byref as _byref,
    c_size_t as _c_size_t,
    c_ulong as _c_ulong,
    c_ulonglong as _c_ulonglong,
    sizeof as _sizeof,
)
from io import open as _io_open
from json import dumps as _json_dumps
from os import getpid as _getpid, name as _os_name
from pathlib import Path
import re
from shutil import disk_usage as _disk_usage
from sys import platform as _sys_platform
from threading import active_count
from time import perf_counter_ns, process_time_ns
from types import FunctionType
from typing import Callable
from uuid import UUID

from autotrade_runtime.artifacts import ArtifactStore

from .performance_qualification import RuntimeBudgetSpec
from .persistence import (
    JournalStore,
    require_exact_journal_store_authority,
)
from .runtime_target_host_campaign_authority import RuntimeTargetHostCampaignAuthority
from .runtime_target_host_inventory import PublishedRuntimeTargetHostInventory
from .runtime_target_host_measurement import (
    RuntimeTargetHostMeasurementArtifact,
    TargetHostFinancialSample,
    TargetHostResearchSample,
)
from .runtime_target_host_measurement_publication import (
    PublishedRuntimeTargetHostMeasurement,
)
from .runtime_target_host_runner import (
    RuntimeTargetHostRunReceipt,
    RuntimeTargetHostRunResult,
    _capture_artifact_store_authority,
    _capture_callable_authority,
    _require_artifact_store_authority,
    _require_callable_authority,
    run_declared_target_host_campaign,
)

_SCHEMA_VERSION = "1.1.0"
_EVIDENCE_TYPE = "AUTOTRADE_TARGET_HOST_RESOURCE_EVIDENCE_CURRENT"
_EVIDENCE_KIND = "RUNTIME_TARGET_HOST_RESOURCE_EVIDENCE_CURRENT"
_JSON_MEDIA_TYPE = "application/json"
_RESOURCE_EVIDENCE_STATUS = "COLLECTED_PROCESS_DISK_V1"
_UNCLOSED_AUTHORITIES = (
    "independent_chronology",
    "provider_source_clock_freshness",
    "signed_terminal_qualification",
)
_EVIDENCE_TOKEN = object()
_ISSUER_TOKEN = object()
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

if _os_name == "nt":  # pragma: no branch - platform import boundary
    from ctypes import windll as _windll

    _get_current_process = _windll.kernel32.GetCurrentProcess
    _get_process_memory_info = _windll.psapi.GetProcessMemoryInfo
    _get_process_io_counters = _windll.kernel32.GetProcessIoCounters
else:
    _get_current_process = None
    _get_process_memory_info = None
    _get_process_io_counters = None

try:
    import resource as _resource
except ImportError:  # pragma: no cover - Windows
    _getrusage = None
    _rusage_self = None
else:
    _getrusage = _resource.getrusage
    _rusage_self = _resource.RUSAGE_SELF


class RuntimeTargetHostResourceEvidenceError(ValueError):
    """Raised when target-host resource evidence cannot be established safely."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeTargetHostResourceEvidenceError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _sha(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    if _SHA256.fullmatch(value) is None:
        raise RuntimeTargetHostResourceEvidenceError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return value


def _git_sha(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    if _GIT_SHA.fullmatch(value) is None:
        raise RuntimeTargetHostResourceEvidenceError(
            f"{name} must be a lowercase 40- or 64-character Git SHA"
        )
    return value


def _non_negative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise RuntimeTargetHostResourceEvidenceError(
            f"{name} must be a non-negative integer"
        )
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise RuntimeTargetHostResourceEvidenceError(
            f"{name} must be a positive integer"
        )
    return value


def _uuid(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    try:
        canonical = str(UUID(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise RuntimeTargetHostResourceEvidenceError(
            f"{name} must be a canonical UUID"
        ) from error
    if canonical != value:
        raise RuntimeTargetHostResourceEvidenceError(
            f"{name} must be a canonical UUID"
        )
    return value


def _canonical_bytes(value: object) -> bytes:
    return (
        _json_dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _windows_peak_rss_bytes() -> int:
    class PROCESS_MEMORY_COUNTERS(_Structure):
        _fields_ = (
            ("cb", _c_ulong),
            ("PageFaultCount", _c_ulong),
            ("PeakWorkingSetSize", _c_size_t),
            ("WorkingSetSize", _c_size_t),
            ("QuotaPeakPagedPoolUsage", _c_size_t),
            ("QuotaPagedPoolUsage", _c_size_t),
            ("QuotaPeakNonPagedPoolUsage", _c_size_t),
            ("QuotaNonPagedPoolUsage", _c_size_t),
            ("PagefileUsage", _c_size_t),
            ("PeakPagefileUsage", _c_size_t),
        )

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = _sizeof(PROCESS_MEMORY_COUNTERS)
    process = _get_current_process()
    ok = _get_process_memory_info(process, _byref(counters), counters.cb)
    if not ok:
        raise OSError("GetProcessMemoryInfo failed")
    return int(counters.PeakWorkingSetSize)


def _peak_rss_bytes() -> int:
    if _os_name == "nt":
        return _positive_int(_windows_peak_rss_bytes(), name="process peak RSS")
    if _getrusage is None or _rusage_self is None:
        raise RuntimeTargetHostResourceEvidenceError(
            "process peak RSS is unavailable on this target host"
        )
    raw = _getrusage(_rusage_self).ru_maxrss
    if type(raw) not in (int, float) or raw <= 0:
        raise RuntimeTargetHostResourceEvidenceError(
            "process peak RSS is unavailable on this target host"
        )
    return _positive_int(
        int(raw * (1 if _sys_platform == "darwin" else 1024)),
        name="process peak RSS",
    )


def _windows_process_io_bytes() -> tuple[int, int]:
    class IO_COUNTERS(_Structure):
        _fields_ = (
            ("ReadOperationCount", _c_ulonglong),
            ("WriteOperationCount", _c_ulonglong),
            ("OtherOperationCount", _c_ulonglong),
            ("ReadTransferCount", _c_ulonglong),
            ("WriteTransferCount", _c_ulonglong),
            ("OtherTransferCount", _c_ulonglong),
        )

    counters = IO_COUNTERS()
    process = _get_current_process()
    if not _get_process_io_counters(process, _byref(counters)):
        raise OSError("GetProcessIoCounters failed")
    return int(counters.ReadTransferCount), int(counters.WriteTransferCount)


def _linux_process_io_bytes() -> tuple[int, int]:
    try:
        with _io_open("/proc/self/io", "r", encoding="ascii") as handle:
            raw = handle.read()
    except OSError as error:
        raise RuntimeTargetHostResourceEvidenceError(
            "process I/O counters are unavailable on this Linux host"
        ) from error
    values: dict[str, int] = {}
    for line in raw.splitlines():
        key, separator, value = line.partition(":")
        if separator != ":" or key not in {"read_bytes", "write_bytes"}:
            continue
        try:
            parsed = int(value.strip())
        except ValueError as error:
            raise RuntimeTargetHostResourceEvidenceError(
                "process I/O counters are malformed"
            ) from error
        values[key] = _non_negative_int(parsed, name=f"process {key}")
    if set(values) != {"read_bytes", "write_bytes"}:
        raise RuntimeTargetHostResourceEvidenceError(
            "process I/O counters are incomplete"
        )
    return values["read_bytes"], values["write_bytes"]


def _process_io_bytes() -> tuple[int, int]:
    if _os_name == "nt":
        values = _windows_process_io_bytes()
    elif _sys_platform.startswith("linux"):
        values = _linux_process_io_bytes()
    else:  # pragma: no cover - unsupported target host
        raise RuntimeTargetHostResourceEvidenceError(
            "process I/O counters are unavailable on this target host"
        )
    return (
        _non_negative_int(values[0], name="process read bytes"),
        _non_negative_int(values[1], name="process write bytes"),
    )


_RESOURCE_PLATFORM_AUTHORITY = tuple(
    (
        dependency_name,
        binding_name,
        dependency,
        _capture_callable_authority(dependency),
    )
    for dependency_name, binding_name, dependency in (
        ("disk usage", "_disk_usage", _disk_usage),
        ("process I/O", "_process_io_bytes", _process_io_bytes),
        ("peak RSS", "_peak_rss_bytes", _peak_rss_bytes),
        ("process identity", "_getpid", _getpid),
        ("monotonic clock", "perf_counter_ns", perf_counter_ns),
        ("process CPU clock", "process_time_ns", process_time_ns),
        ("thread count", "active_count", active_count),
    )
)


_RESOURCE_PLATFORM_ATTRIBUTE_AUTHORITY = ()
if type(_disk_usage) is FunctionType:
    _disk_usage_globals = _disk_usage.__globals__
    _disk_usage_os_module = _disk_usage_globals.get("os")
    if (
        "statvfs" in _disk_usage.__code__.co_names
        and _disk_usage_os_module is not None
    ):
        _disk_usage_os_namespace = vars(_disk_usage_os_module)
        if "statvfs" in _disk_usage_os_namespace:
            _RESOURCE_PLATFORM_ATTRIBUTE_AUTHORITY = (
                (
                    "disk usage os.statvfs",
                    _disk_usage_os_namespace,
                    "statvfs",
                    dict.__getitem__(_disk_usage_os_namespace, "statvfs"),
                ),
            )


_RESOURCE_PLATFORM_CLASS_FUNCTION_AUTHORITY = ()
if type(_disk_usage) is FunctionType:
    _disk_usage_result_type = _disk_usage.__globals__.get("_ntuple_diskusage")
    if type(_disk_usage_result_type) is type:
        _disk_usage_result_new = vars(_disk_usage_result_type).get("__new__")
        if type(_disk_usage_result_new) is FunctionType:
            _RESOURCE_PLATFORM_CLASS_FUNCTION_AUTHORITY = (
                (
                    "disk usage result constructor",
                    _disk_usage_result_type,
                    "__new__",
                    _disk_usage_result_new,
                    _capture_callable_authority(_disk_usage_result_new),
                ),
            )

_RESOURCE_JOURNAL_STORE_TYPE = JournalStore
_RESOURCE_JOURNAL_AUTHORITY_CHECK = require_exact_journal_store_authority
_RESOURCE_OUTBOX_BACKLOG_CUT = JournalStore.outbox_backlog_cut
_RESOURCE_OUTBOX_BACKLOG_TAIL_CUT = JournalStore.outbox_backlog_tail_cut
_RESOURCE_OUTBOX_BACKLOG_HIGH_WATER = JournalStore.outbox_backlog_high_water_since
_RESOURCE_JOURNAL_BACKLOG_DEPENDENCIES = (
    ("JournalStore connection", "_connect", JournalStore._connect),
    (
        "JournalStore Windows connection",
        "_connect_windows",
        JournalStore._connect_windows,
    ),
    (
        "JournalStore backlog transition writer",
        "_append_outbox_backlog_transition",
        JournalStore._append_outbox_backlog_transition,
    ),
    (
        "JournalStore backlog transition tail",
        "_outbox_transition_tail_value",
        JournalStore._outbox_transition_tail_value,
    ),
    (
        "JournalStore outbox transition sequence",
        "_outbox_transition_sequence_value",
        JournalStore._outbox_transition_sequence_value,
    ),
    (
        "JournalStore pending outbox count",
        "_pending_outbox_count_value",
        JournalStore._pending_outbox_count_value,
    ),
)
_RESOURCE_JOURNAL_CALLABLE_AUTHORITY = (
    (
        "JournalStore authority verifier",
        _RESOURCE_JOURNAL_AUTHORITY_CHECK,
        _capture_callable_authority(_RESOURCE_JOURNAL_AUTHORITY_CHECK),
    ),
    (
        "JournalStore outbox backlog cut",
        _RESOURCE_OUTBOX_BACKLOG_CUT,
        _capture_callable_authority(_RESOURCE_OUTBOX_BACKLOG_CUT),
    ),
    (
        "JournalStore outbox backlog tail cut",
        _RESOURCE_OUTBOX_BACKLOG_TAIL_CUT,
        _capture_callable_authority(_RESOURCE_OUTBOX_BACKLOG_TAIL_CUT),
    ),
    (
        "JournalStore outbox backlog high-water",
        _RESOURCE_OUTBOX_BACKLOG_HIGH_WATER,
        _capture_callable_authority(_RESOURCE_OUTBOX_BACKLOG_HIGH_WATER),
    ),
    *(
        (
            dependency_name,
            dependency,
            _capture_callable_authority(dependency),
        )
        for dependency_name, _attribute_name, dependency
        in _RESOURCE_JOURNAL_BACKLOG_DEPENDENCIES
    ),
)


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostResourceSnapshot:
    process_id: int
    monotonic_ns: int
    process_cpu_ns: int
    peak_rss_bytes: int
    io_read_bytes: int
    io_write_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    thread_count: int

    def __post_init__(self) -> None:
        _positive_int(self.process_id, name="process_id")
        _non_negative_int(self.monotonic_ns, name="monotonic_ns")
        _non_negative_int(self.process_cpu_ns, name="process_cpu_ns")
        _positive_int(self.peak_rss_bytes, name="peak_rss_bytes")
        _non_negative_int(self.io_read_bytes, name="io_read_bytes")
        _non_negative_int(self.io_write_bytes, name="io_write_bytes")
        total = _positive_int(self.disk_total_bytes, name="disk_total_bytes")
        free = _non_negative_int(self.disk_free_bytes, name="disk_free_bytes")
        if free > total:
            raise RuntimeTargetHostResourceEvidenceError(
                "disk_free_bytes cannot exceed disk_total_bytes"
            )
        _positive_int(self.thread_count, name="thread_count")

    @property
    def payload(self) -> dict[str, int]:
        return {
            "process_id": self.process_id,
            "monotonic_ns": self.monotonic_ns,
            "process_cpu_ns": self.process_cpu_ns,
            "peak_rss_bytes": self.peak_rss_bytes,
            "io_read_bytes": self.io_read_bytes,
            "io_write_bytes": self.io_write_bytes,
            "disk_total_bytes": self.disk_total_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "thread_count": self.thread_count,
        }


def capture_runtime_target_host_resource_snapshot(
    *, evidence_root: Path
) -> RuntimeTargetHostResourceSnapshot:
    """Capture a provider-free resource cut without consulting wall clock."""

    if type(evidence_root) is not type(Path()):
        raise TypeError("evidence_root must be an exact pathlib path")
    try:
        usage = _disk_usage(evidence_root)
    except OSError as error:
        raise RuntimeTargetHostResourceEvidenceError(
            "target-host evidence filesystem usage is unavailable"
        ) from error
    read_bytes, write_bytes = _process_io_bytes()
    return RuntimeTargetHostResourceSnapshot(
        process_id=_positive_int(_getpid(), name="process_id"),
        monotonic_ns=_non_negative_int(perf_counter_ns(), name="monotonic_ns"),
        process_cpu_ns=_non_negative_int(process_time_ns(), name="process_cpu_ns"),
        peak_rss_bytes=_peak_rss_bytes(),
        io_read_bytes=read_bytes,
        io_write_bytes=write_bytes,
        disk_total_bytes=_positive_int(int(usage.total), name="disk_total_bytes"),
        disk_free_bytes=_non_negative_int(int(usage.free), name="disk_free_bytes"),
        thread_count=_positive_int(active_count(), name="thread_count"),
    )


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostResourceEvidence:
    authority_id: str
    authority_digest: str
    source_sha: str
    release_artifact_id: str
    release_artifact_sha256: str
    scenario_id: str
    spec_digest: str
    host_fingerprint: str
    inventory_artifact_id: str
    inventory_payload_sha256: str
    measurement_artifact_id: str
    measurement_payload_sha256: str
    run_receipt_artifact_id: str
    run_receipt_payload_sha256: str
    outbox_transition_start_sequence: int
    outbox_transition_end_sequence: int
    outbox_backlog_start_pending_count: int
    outbox_backlog_end_pending_count: int
    queue_backlog_high_water: int
    reconnect_backlog_remaining: int
    before: RuntimeTargetHostResourceSnapshot
    after: RuntimeTargetHostResourceSnapshot
    resource_evidence_status: str = _RESOURCE_EVIDENCE_STATUS
    terminal_qualification_eligible: bool = False
    schema_version: str = _SCHEMA_VERSION
    evidence_type: str = _EVIDENCE_TYPE
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _EVIDENCE_TOKEN:
            raise RuntimeTargetHostResourceEvidenceError(
                "resource evidence must come from canonical issuer"
            )
        _text(self.authority_id, name="authority_id")
        _sha(self.authority_digest, name="authority_digest")
        _git_sha(self.source_sha, name="source_sha")
        _uuid(self.release_artifact_id, name="release_artifact_id")
        _sha(self.release_artifact_sha256, name="release_artifact_sha256")
        _text(self.scenario_id, name="scenario_id")
        _sha(self.spec_digest, name="spec_digest")
        _sha(self.host_fingerprint, name="host_fingerprint")
        _uuid(self.inventory_artifact_id, name="inventory_artifact_id")
        _sha(self.inventory_payload_sha256, name="inventory_payload_sha256")
        _uuid(self.measurement_artifact_id, name="measurement_artifact_id")
        _sha(self.measurement_payload_sha256, name="measurement_payload_sha256")
        _uuid(self.run_receipt_artifact_id, name="run_receipt_artifact_id")
        _sha(self.run_receipt_payload_sha256, name="run_receipt_payload_sha256")
        if type(self.before) is not RuntimeTargetHostResourceSnapshot:
            raise TypeError("before must be exact RuntimeTargetHostResourceSnapshot")
        if type(self.after) is not RuntimeTargetHostResourceSnapshot:
            raise TypeError("after must be exact RuntimeTargetHostResourceSnapshot")
        if self.after.process_id != self.before.process_id:
            raise RuntimeTargetHostResourceEvidenceError(
                "process identity changed during target-host run"
            )
        if self.after.monotonic_ns < self.before.monotonic_ns:
            raise RuntimeTargetHostResourceEvidenceError(
                "resource monotonic cut moved backwards"
            )
        if self.after.process_cpu_ns < self.before.process_cpu_ns:
            raise RuntimeTargetHostResourceEvidenceError(
                "process CPU counter moved backwards"
            )
        if self.after.io_read_bytes < self.before.io_read_bytes:
            raise RuntimeTargetHostResourceEvidenceError(
                "process read-byte counter moved backwards"
            )
        if self.after.io_write_bytes < self.before.io_write_bytes:
            raise RuntimeTargetHostResourceEvidenceError(
                "process write-byte counter moved backwards"
            )
        if self.after.peak_rss_bytes < self.before.peak_rss_bytes:
            raise RuntimeTargetHostResourceEvidenceError(
                "process peak RSS counter moved backwards"
            )
        if self.after.disk_total_bytes != self.before.disk_total_bytes:
            raise RuntimeTargetHostResourceEvidenceError(
                "evidence filesystem capacity changed during target-host run"
            )
        transition_start = _non_negative_int(
            self.outbox_transition_start_sequence,
            name="outbox_transition_start_sequence",
        )
        transition_end = _non_negative_int(
            self.outbox_transition_end_sequence,
            name="outbox_transition_end_sequence",
        )
        if transition_end < transition_start:
            raise RuntimeTargetHostResourceEvidenceError(
                "outbox transition end cannot precede campaign start"
            )
        backlog_start = _non_negative_int(
            self.outbox_backlog_start_pending_count,
            name="outbox_backlog_start_pending_count",
        )
        backlog_end = _non_negative_int(
            self.outbox_backlog_end_pending_count,
            name="outbox_backlog_end_pending_count",
        )
        high_water = _non_negative_int(
            self.queue_backlog_high_water,
            name="queue_backlog_high_water",
        )
        reconnect_backlog = _non_negative_int(
            self.reconnect_backlog_remaining,
            name="reconnect_backlog_remaining",
        )
        if backlog_end != reconnect_backlog:
            raise RuntimeTargetHostResourceEvidenceError(
                "outbox backlog end does not match reconnect backlog authority"
            )
        if high_water < max(backlog_start, backlog_end):
            raise RuntimeTargetHostResourceEvidenceError(
                "queue backlog high-water is below an observed campaign endpoint"
            )
        transition_count = transition_end - transition_start
        pending_delta = backlog_end - backlog_start
        if (
            abs(pending_delta) > transition_count
            or (transition_count - pending_delta) % 2 != 0
        ):
            raise RuntimeTargetHostResourceEvidenceError(
                "outbox backlog endpoints are incompatible with transition count"
            )
        enqueue_count = (transition_count + pending_delta) // 2
        if high_water > backlog_start + enqueue_count:
            raise RuntimeTargetHostResourceEvidenceError(
                "queue backlog high-water exceeds causal enqueue authority"
            )
        if self.resource_evidence_status != _RESOURCE_EVIDENCE_STATUS:
            raise RuntimeTargetHostResourceEvidenceError(
                "resource evidence status is not canonical"
            )
        if self.terminal_qualification_eligible is not False:
            raise RuntimeTargetHostResourceEvidenceError(
                "raw resource evidence cannot be terminal qualification"
            )
        if self.schema_version != _SCHEMA_VERSION:
            raise RuntimeTargetHostResourceEvidenceError(
                "unsupported resource evidence schema version"
            )
        if self.evidence_type != _EVIDENCE_TYPE:
            raise RuntimeTargetHostResourceEvidenceError(
                "unsupported resource evidence type"
            )

    @property
    def elapsed_monotonic_ns(self) -> int:
        return self.after.monotonic_ns - self.before.monotonic_ns

    @property
    def process_cpu_delta_ns(self) -> int:
        return self.after.process_cpu_ns - self.before.process_cpu_ns

    @property
    def io_read_delta_bytes(self) -> int:
        return self.after.io_read_bytes - self.before.io_read_bytes

    @property
    def io_write_delta_bytes(self) -> int:
        return self.after.io_write_bytes - self.before.io_write_bytes

    @property
    def disk_free_delta_bytes(self) -> int:
        return self.after.disk_free_bytes - self.before.disk_free_bytes

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evidence_type": self.evidence_type,
            "authority_id": self.authority_id,
            "authority_digest": self.authority_digest,
            "source_sha": self.source_sha,
            "release_artifact_id": self.release_artifact_id,
            "release_artifact_sha256": self.release_artifact_sha256,
            "scenario_id": self.scenario_id,
            "spec_digest": self.spec_digest,
            "host_fingerprint": self.host_fingerprint,
            "inventory_artifact_id": self.inventory_artifact_id,
            "inventory_payload_sha256": self.inventory_payload_sha256,
            "measurement_artifact_id": self.measurement_artifact_id,
            "measurement_payload_sha256": self.measurement_payload_sha256,
            "run_receipt_artifact_id": self.run_receipt_artifact_id,
            "run_receipt_payload_sha256": self.run_receipt_payload_sha256,
            "resource_evidence_status": self.resource_evidence_status,
            "terminal_qualification_eligible": self.terminal_qualification_eligible,
            "unclosed_authorities": list(_UNCLOSED_AUTHORITIES),
            "outbox_backlog": {
                "transition_start_sequence": self.outbox_transition_start_sequence,
                "transition_end_sequence": self.outbox_transition_end_sequence,
                "start_pending_count": self.outbox_backlog_start_pending_count,
                "end_pending_count": self.outbox_backlog_end_pending_count,
                "high_water": self.queue_backlog_high_water,
            },
            "reconnect_backlog_remaining": self.reconnect_backlog_remaining,
            "before": self.before.payload,
            "after": self.after.payload,
            "derived": {
                "process_id": self.after.process_id,
                "elapsed_monotonic_ns": self.elapsed_monotonic_ns,
                "process_cpu_delta_ns": self.process_cpu_delta_ns,
                "io_read_delta_bytes": self.io_read_delta_bytes,
                "io_write_delta_bytes": self.io_write_delta_bytes,
                "peak_rss_bytes": self.after.peak_rss_bytes,
                "disk_free_delta_bytes": self.disk_free_delta_bytes,
                "thread_count_start": self.before.thread_count,
                "thread_count_end": self.after.thread_count,
                "queue_backlog_high_water": self.queue_backlog_high_water,
            },
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.canonical_payload())

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class PublishedRuntimeTargetHostResourceEvidence:
    artifact_id: str
    payload_sha256: str
    resource_evidence_status: str


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostResourceRunResult:
    run: RuntimeTargetHostRunResult
    resource_evidence: RuntimeTargetHostResourceEvidence
    published_resource_evidence: PublishedRuntimeTargetHostResourceEvidence

    @property
    def terminal_qualification_eligible(self) -> bool:
        return False


def _capture_descriptor_authority(
    owner: type,
    names: tuple[str, ...],
) -> tuple[tuple[type, str, object, FunctionType | None, object | None], ...]:
    namespace = type.__getattribute__(owner, "__dict__")
    result: list[
        tuple[type, str, object, FunctionType | None, object | None]
    ] = []
    for name in names:
        descriptor = namespace[name]
        function: FunctionType | None
        if type(descriptor) is FunctionType:
            function = descriptor
        elif type(descriptor) is property and type(descriptor.fget) is FunctionType:
            function = descriptor.fget
        elif type(descriptor) is classmethod and type(descriptor.__func__) is FunctionType:
            function = descriptor.__func__
        else:
            function = None
        result.append(
            (
                owner,
                name,
                descriptor,
                function,
                function.__code__ if function is not None else None,
            )
        )
    return tuple(result)


_RESOURCE_DESCRIPTOR_SPECS = (
    (
        RuntimeTargetHostResourceSnapshot,
        (
            "process_id",
            "monotonic_ns",
            "process_cpu_ns",
            "peak_rss_bytes",
            "io_read_bytes",
            "io_write_bytes",
            "disk_total_bytes",
            "disk_free_bytes",
            "thread_count",
            "__init__",
            "__post_init__",
            "payload",
        ),
    ),
    (
        RuntimeTargetHostResourceEvidence,
        (
            "authority_id",
            "authority_digest",
            "source_sha",
            "release_artifact_id",
            "release_artifact_sha256",
            "scenario_id",
            "spec_digest",
            "host_fingerprint",
            "inventory_artifact_id",
            "inventory_payload_sha256",
            "measurement_artifact_id",
            "measurement_payload_sha256",
            "run_receipt_artifact_id",
            "run_receipt_payload_sha256",
            "outbox_transition_start_sequence",
            "outbox_transition_end_sequence",
            "outbox_backlog_start_pending_count",
            "outbox_backlog_end_pending_count",
            "queue_backlog_high_water",
            "reconnect_backlog_remaining",
            "before",
            "after",
            "resource_evidence_status",
            "terminal_qualification_eligible",
            "schema_version",
            "evidence_type",
            "__init__",
            "__post_init__",
            "elapsed_monotonic_ns",
            "process_cpu_delta_ns",
            "io_read_delta_bytes",
            "io_write_delta_bytes",
            "disk_free_delta_bytes",
            "canonical_payload",
            "canonical_bytes",
            "digest",
        ),
    ),
    (
        PublishedRuntimeTargetHostResourceEvidence,
        (
            "artifact_id",
            "payload_sha256",
            "resource_evidence_status",
            "__init__",
        ),
    ),
    (
        RuntimeTargetHostResourceRunResult,
        (
            "run",
            "resource_evidence",
            "published_resource_evidence",
            "__init__",
            "terminal_qualification_eligible",
        ),
    ),
    (
        RuntimeTargetHostRunResult,
        (
            "authority",
            "research_plan",
            "measurement",
            "published_inventory",
            "published_measurement",
            "run_receipt",
            "run_receipt_artifact_id",
            "run_receipt_payload_sha256",
            "__init__",
            "terminal_qualification_eligible",
        ),
    ),
    (
        RuntimeTargetHostCampaignAuthority,
        (
            "authority_id",
            "event_id",
            "scenario_id",
            "spec_digest",
            "source_sha",
            "configuration_hash",
            "host_fingerprint",
            "release_artifact_id",
            "release_artifact_sha256",
            "financial_plan_id",
            "financial_plan_digest",
            "store_identity_digest",
            "journal_taxonomy_digest",
            "declared_journal_sequence",
            "payload_hash",
            "__init__",
            "__post_init__",
            "digest",
        ),
    ),
    (
        TargetHostFinancialSample,
        (
            "event_id",
            "event_journal_sequence",
            "measurement_event_id",
            "measurement_journal_sequence",
            "event_payload_hash",
            "monotonic_start_ns",
            "monotonic_end_ns",
            "latency_us",
            "staleness_us",
            "__init__",
            "from_durable",
            "payload",
        ),
    ),
    (
        TargetHostResearchSample,
        (
            "sample_id",
            "phase",
            "measurement_event_id",
            "measurement_journal_sequence",
            "monotonic_start_ns",
            "monotonic_end_ns",
            "interference_us",
            "__init__",
            "from_durable",
            "payload",
        ),
    ),
    (
        RuntimeTargetHostMeasurementArtifact,
        (
            "authority_id",
            "authority_digest",
            "source_sha",
            "release_artifact_id",
            "release_artifact_sha256",
            "scenario_id",
            "spec_digest",
            "configuration_hash",
            "host_fingerprint",
            "financial_plan_id",
            "financial_plan_digest",
            "store_identity_digest",
            "journal_taxonomy_digest",
            "authority_journal_sequence",
            "conservation_start_journal_sequence",
            "conservation_end_journal_sequence",
            "conservation_digest",
            "reconnect_backlog_remaining",
            "financial_samples",
            "research_samples",
            "staleness_basis",
            "resource_evidence_status",
            "schema_version",
            "evidence_type",
            "__init__",
            "__post_init__",
            "terminal_qualification_eligible",
            "financial_event_ids",
            "financial_latency_us",
            "financial_staleness_us",
            "research_interference_us",
            "canonical_payload",
            "canonical_bytes",
            "digest",
        ),
    ),
    (
        PublishedRuntimeTargetHostInventory,
        (
            "artifact_id",
            "payload_sha256",
            "host_fingerprint",
            "collector_id",
            "collector_version",
            "__init__",
        ),
    ),
    (
        PublishedRuntimeTargetHostMeasurement,
        (
            "artifact_id",
            "payload_sha256",
            "authority_id",
            "authority_digest",
            "release_artifact_id",
            "release_artifact_sha256",
            "resource_evidence_status",
            "__init__",
        ),
    ),
    (
        RuntimeTargetHostRunReceipt,
        (
            "authority_id",
            "authority_digest",
            "authority_journal_sequence",
            "financial_plan_id",
            "financial_plan_digest",
            "research_plan_id",
            "research_plan_digest",
            "research_plan_declared_journal_sequence",
            "source_sha",
            "release_artifact_id",
            "release_artifact_sha256",
            "inventory_artifact_id",
            "inventory_payload_sha256",
            "measurement_artifact_id",
            "measurement_payload_sha256",
            "scenario_id",
            "spec_digest",
            "host_fingerprint",
            "resource_evidence_status",
            "terminal_qualification_eligible",
            "schema_version",
            "evidence_type",
            "__init__",
            "canonical_payload",
            "canonical_bytes",
            "digest",
        ),
    ),
)
_RESOURCE_DESCRIPTOR_AUTHORITY = tuple(
    descriptor_state
    for owner, names in _RESOURCE_DESCRIPTOR_SPECS
    for descriptor_state in _capture_descriptor_authority(owner, names)
)
_RESOURCE_CLASS_FUNCTION_AUTHORITY = tuple(
    (
        f"{owner.__name__}.{name}",
        function,
        _capture_callable_authority(function),
    )
    for owner, name, _descriptor, function, _code in _RESOURCE_DESCRIPTOR_AUTHORITY
    if function is not None
)


def issue_runtime_target_host_resource_evidence(
    run: RuntimeTargetHostRunResult,
    *,
    before: RuntimeTargetHostResourceSnapshot,
    after: RuntimeTargetHostResourceSnapshot,
    outbox_backlog: dict[str, int],
    _issuer_token: object | None = None,
) -> RuntimeTargetHostResourceEvidence:
    """Bind resource counters to the exact retained current-run chain.

    The helper is intentionally wrapper-issued: callers cannot turn a
    caller-constructed ``RuntimeTargetHostRunResult`` into retained resource
    evidence merely by matching its outer Python type.
    """

    if _issuer_token is not _ISSUER_TOKEN:
        raise RuntimeTargetHostResourceEvidenceError(
            "resource evidence must be issued by canonical campaign wrapper"
        )
    if type(run) is not RuntimeTargetHostRunResult:
        raise TypeError("run must be exact RuntimeTargetHostRunResult")
    if type(before) is not RuntimeTargetHostResourceSnapshot:
        raise TypeError("before must be exact RuntimeTargetHostResourceSnapshot")
    if type(after) is not RuntimeTargetHostResourceSnapshot:
        raise TypeError("after must be exact RuntimeTargetHostResourceSnapshot")
    if type(outbox_backlog) is not dict or set(outbox_backlog) != {
        "start_transition_sequence",
        "end_transition_sequence",
        "start_pending_count",
        "end_pending_count",
        "high_water",
    }:
        raise TypeError("outbox_backlog must be exact canonical high-water evidence")
    measurement_digest = run.measurement.digest
    if run.published_measurement.payload_sha256 != measurement_digest:
        raise RuntimeTargetHostResourceEvidenceError(
            "retained target-host measurement digest does not match run result"
        )
    receipt_digest = run.run_receipt.digest
    if run.run_receipt_payload_sha256 != receipt_digest:
        raise RuntimeTargetHostResourceEvidenceError(
            "retained target-host run receipt digest does not match run result"
        )
    return RuntimeTargetHostResourceEvidence(
        authority_id=run.authority.authority_id,
        authority_digest=run.authority.digest,
        source_sha=run.authority.source_sha,
        release_artifact_id=run.authority.release_artifact_id,
        release_artifact_sha256=run.authority.release_artifact_sha256,
        scenario_id=run.authority.scenario_id,
        spec_digest=run.authority.spec_digest,
        host_fingerprint=run.authority.host_fingerprint,
        inventory_artifact_id=run.published_inventory.artifact_id,
        inventory_payload_sha256=run.published_inventory.payload_sha256,
        measurement_artifact_id=run.published_measurement.artifact_id,
        measurement_payload_sha256=measurement_digest,
        run_receipt_artifact_id=run.run_receipt_artifact_id,
        run_receipt_payload_sha256=receipt_digest,
        outbox_transition_start_sequence=outbox_backlog[
            "start_transition_sequence"
        ],
        outbox_transition_end_sequence=outbox_backlog[
            "end_transition_sequence"
        ],
        outbox_backlog_start_pending_count=outbox_backlog[
            "start_pending_count"
        ],
        outbox_backlog_end_pending_count=outbox_backlog[
            "end_pending_count"
        ],
        queue_backlog_high_water=outbox_backlog["high_water"],
        reconnect_backlog_remaining=run.measurement.reconnect_backlog_remaining,
        before=before,
        after=after,
        _token=_EVIDENCE_TOKEN,
    )


def publish_runtime_target_host_resource_evidence(
    evidence_store: ArtifactStore,
    *,
    artifact_id: str,
    evidence: RuntimeTargetHostResourceEvidence,
) -> PublishedRuntimeTargetHostResourceEvidence:
    """Retain exact issuer-created resource bytes in the neutral ArtifactStore."""

    if type(evidence_store) is not ArtifactStore:
        raise TypeError("evidence_store must be exact ArtifactStore")
    if type(evidence) is not RuntimeTargetHostResourceEvidence:
        raise TypeError("evidence must be exact RuntimeTargetHostResourceEvidence")
    artifact_id = _uuid(artifact_id, name="resource_artifact_id")
    raw = evidence.canonical_bytes()
    manifest = ArtifactStore.publish_bytes(
        evidence_store,
        artifact_id=artifact_id,
        data=raw,
        media_type=_JSON_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        source_refs=[f"git:{evidence.source_sha}"],
        metadata={
            "evidence_kind": _EVIDENCE_KIND,
            "schema_version": evidence.schema_version,
            "authority_id": evidence.authority_id,
            "authority_digest": evidence.authority_digest,
            "host_fingerprint": evidence.host_fingerprint,
            "resource_evidence_status": evidence.resource_evidence_status,
            "terminal_qualification_eligible": False,
        },
    )
    if (
        manifest.get("artifact_id") != artifact_id
        or manifest.get("sha256") != evidence.digest
        or manifest.get("media_type") != _JSON_MEDIA_TYPE
    ):
        raise RuntimeTargetHostResourceEvidenceError(
            "retained target-host resource evidence changed during publication"
        )
    return PublishedRuntimeTargetHostResourceEvidence(
        artifact_id=artifact_id,
        payload_sha256=evidence.digest,
        resource_evidence_status=evidence.resource_evidence_status,
    )


def run_declared_target_host_campaign_with_resources(
    *,
    journal: JournalStore,
    evidence_store: ArtifactStore,
    spec: RuntimeBudgetSpec,
    authority_id: str,
    research_plan_id: str,
    financial_operations: dict[str, Callable[[], object]],
    research_operations: dict[str, Callable[[], object]],
    inventory_artifact_id: str,
    measurement_artifact_id: str,
    run_receipt_artifact_id: str,
    resource_artifact_id: str,
) -> RuntimeTargetHostResourceRunResult:
    """Run the canonical campaign and retain bounded process/disk evidence."""

    resource_artifact_id = _uuid(
        resource_artifact_id, name="resource_artifact_id"
    )
    existing_ids = {
        _uuid(inventory_artifact_id, name="inventory_artifact_id"),
        _uuid(measurement_artifact_id, name="measurement_artifact_id"),
        _uuid(run_receipt_artifact_id, name="run_receipt_artifact_id"),
    }
    if resource_artifact_id in existing_ids:
        raise RuntimeTargetHostResourceEvidenceError(
            "resource evidence artifact ID must be distinct from existing run artifacts"
        )
    if type(evidence_store) is not ArtifactStore:
        raise TypeError("evidence_store must be exact ArtifactStore")
    if type(journal) is not _RESOURCE_JOURNAL_STORE_TYPE:
        raise TypeError("journal must be exact JournalStore")

    journal_store_type = _RESOURCE_JOURNAL_STORE_TYPE
    journal_authority_check = _RESOURCE_JOURNAL_AUTHORITY_CHECK
    outbox_backlog_cut = _RESOURCE_OUTBOX_BACKLOG_CUT
    outbox_backlog_tail_cut = _RESOURCE_OUTBOX_BACKLOG_TAIL_CUT
    outbox_backlog_high_water = _RESOURCE_OUTBOX_BACKLOG_HIGH_WATER
    journal_backlog_dependencies = _RESOURCE_JOURNAL_BACKLOG_DEPENDENCIES
    journal_callable_states = _RESOURCE_JOURNAL_CALLABLE_AUTHORITY
    store_authority = _capture_artifact_store_authority(evidence_store)
    resource_platform_states = _RESOURCE_PLATFORM_AUTHORITY
    resource_platform_attribute_states = _RESOURCE_PLATFORM_ATTRIBUTE_AUTHORITY
    resource_platform_class_function_states = (
        _RESOURCE_PLATFORM_CLASS_FUNCTION_AUTHORITY
    )
    capture_snapshot = capture_runtime_target_host_resource_snapshot
    issue_evidence = issue_runtime_target_host_resource_evidence
    publish_evidence = publish_runtime_target_host_resource_evidence
    read_snapshot = ArtifactStore.read_authenticated_snapshot
    artifact_publish = ArtifactStore.publish_bytes
    runner = run_declared_target_host_campaign
    capture_state = _capture_callable_authority(capture_snapshot)
    issue_state = _capture_callable_authority(issue_evidence)
    publish_state = _capture_callable_authority(publish_evidence)
    read_state = _capture_callable_authority(read_snapshot)
    artifact_publish_state = _capture_callable_authority(artifact_publish)
    runner_state = _capture_callable_authority(runner)

    require_callable = _require_callable_authority
    require_store = _require_artifact_store_authority
    require_callable_state = _capture_callable_authority(require_callable)
    require_store_state = _capture_callable_authority(require_store)
    require_callable_code = require_callable.__code__
    require_store_code = require_store.__code__
    module_namespace = globals()
    raw_dict_getitem = dict.__getitem__
    raw_object_getattribute = object.__getattribute__
    raw_type_getattribute = type.__getattribute__
    descriptor_namespace_type = type(
        raw_type_getattribute(RuntimeTargetHostResourceEvidence, "__dict__")
    )
    raw_descriptor_getitem = descriptor_namespace_type.__getitem__
    descriptor_states = _RESOURCE_DESCRIPTOR_AUTHORITY
    class_function_states = _RESOURCE_CLASS_FUNCTION_AUTHORITY

    def require_resource_platform_authority(*, phase: str) -> None:
        for (
            dependency_name,
            binding_name,
            dependency,
            dependency_state,
        ) in resource_platform_states:
            if raw_dict_getitem(module_namespace, binding_name) is not dependency:
                raise RuntimeTargetHostResourceEvidenceError(
                    "resource platform dependency changed "
                    f"{phase}: {dependency_name}"
                )
            try:
                require_callable(
                    dependency,
                    dependency_state,
                    name=f"resource platform dependency {dependency_name}",
                )
            except ValueError as error:
                raise RuntimeTargetHostResourceEvidenceError(
                    "resource platform dependency changed "
                    f"{phase}: {dependency_name}"
                ) from error
        for (
            dependency_name,
            namespace,
            attribute_name,
            expected,
        ) in resource_platform_attribute_states:
            try:
                current = raw_dict_getitem(namespace, attribute_name)
            except KeyError as error:
                raise RuntimeTargetHostResourceEvidenceError(
                    "resource platform dependency changed "
                    f"{phase}: {dependency_name}"
                ) from error
            if current is not expected:
                raise RuntimeTargetHostResourceEvidenceError(
                    "resource platform dependency changed "
                    f"{phase}: {dependency_name}"
                )


        for (
            dependency_name,
            owner,
            attribute_name,
            function,
            function_state,
        ) in resource_platform_class_function_states:
            namespace = raw_type_getattribute(owner, "__dict__")
            current = raw_descriptor_getitem(namespace, attribute_name)
            if current is not function:
                raise RuntimeTargetHostResourceEvidenceError(
                    "resource platform dependency changed "
                    f"{phase}: {dependency_name}"
                )
            try:
                require_callable(
                    function,
                    function_state,
                    name=f"resource platform dependency {dependency_name}",
                )
            except ValueError as error:
                raise RuntimeTargetHostResourceEvidenceError(
                    "resource platform dependency changed "
                    f"{phase}: {dependency_name}"
                ) from error


    def require_resource_journal_authority(*, phase: str) -> None:
        if raw_dict_getitem(module_namespace, "JournalStore") is not journal_store_type:
            raise RuntimeTargetHostResourceEvidenceError(
                "resource JournalStore type changed " + phase
            )
        if (
            raw_dict_getitem(
                module_namespace,
                "require_exact_journal_store_authority",
            )
            is not journal_authority_check
        ):
            raise RuntimeTargetHostResourceEvidenceError(
                "resource JournalStore authority verifier changed " + phase
            )
        if (
            raw_type_getattribute(
                journal_store_type,
                "outbox_backlog_cut",
            )
            is not outbox_backlog_cut
            or raw_type_getattribute(
                journal_store_type,
                "outbox_backlog_tail_cut",
            )
            is not outbox_backlog_tail_cut
            or raw_type_getattribute(
                journal_store_type,
                "outbox_backlog_high_water_since",
            )
            is not outbox_backlog_high_water
        ):
            raise RuntimeTargetHostResourceEvidenceError(
                "resource JournalStore backlog authority changed " + phase
            )
        for dependency_name, attribute_name, dependency in journal_backlog_dependencies:
            if (
                raw_type_getattribute(journal_store_type, attribute_name)
                is not dependency
            ):
                raise RuntimeTargetHostResourceEvidenceError(
                    "resource JournalStore backlog dependency changed "
                    f"{phase}: {dependency_name}"
                )
        for name, function, state in journal_callable_states:
            try:
                require_callable(function, state, name=name)
            except ValueError as error:
                raise RuntimeTargetHostResourceEvidenceError(
                    "resource JournalStore executable changed "
                    f"{phase}: {name}"
                ) from error
        journal_authority_check(
            journal,
            subject="resource evidence JournalStore",
        )

    def require_resource_class_authority(*, phase: str) -> None:
        for owner, name, descriptor, function, code in descriptor_states:
            namespace = raw_type_getattribute(owner, "__dict__")
            current = raw_descriptor_getitem(namespace, name)
            if current is not descriptor:
                raise RuntimeTargetHostResourceEvidenceError(
                    "resource evidence class descriptor changed "
                    f"{phase}: {owner.__name__}.{name}"
                )
            if (
                function is not None
                and raw_object_getattribute(function, "__code__") is not code
            ):
                raise RuntimeTargetHostResourceEvidenceError(
                    "resource evidence class executable changed "
                    f"{phase}: {owner.__name__}.{name}"
                )
        for name, function, state in class_function_states:
            require_callable(function, state, name=name)

    require_resource_journal_authority(phase="before target-host run")
    require_resource_platform_authority(phase="before target-host run")
    require_resource_class_authority(phase="before target-host run")
    backlog_start = outbox_backlog_cut(journal)
    before = capture_snapshot(evidence_root=evidence_store.root)
    backlog_start_after_snapshot = outbox_backlog_tail_cut(
        journal,
        start_transition_sequence=backlog_start["transition_sequence"],
        start_pending_count=backlog_start["pending_count"],
    )
    if backlog_start_after_snapshot != backlog_start:
        raise RuntimeTargetHostResourceEvidenceError(
            "outbox backlog changed while opening the resource measurement cut"
        )
    run = runner(
        journal=journal,
        evidence_store=evidence_store,
        spec=spec,
        authority_id=authority_id,
        research_plan_id=research_plan_id,
        financial_operations=financial_operations,
        research_operations=research_operations,
        inventory_artifact_id=inventory_artifact_id,
        measurement_artifact_id=measurement_artifact_id,
        run_receipt_artifact_id=run_receipt_artifact_id,
    )

    if (
        raw_dict_getitem(module_namespace, "_require_callable_authority")
        is not require_callable
        or raw_object_getattribute(require_callable, "__code__")
        is not require_callable_code
    ):
        raise RuntimeTargetHostResourceEvidenceError(
            "resource callable verifier changed during target-host run"
        )
    if (
        raw_dict_getitem(module_namespace, "_require_artifact_store_authority")
        is not require_store
        or raw_object_getattribute(require_store, "__code__")
        is not require_store_code
    ):
        raise RuntimeTargetHostResourceEvidenceError(
            "resource ArtifactStore verifier changed during target-host run"
        )
    require_callable(
        require_callable,
        require_callable_state,
        name="resource callable verifier",
    )
    require_callable(
        require_store,
        require_store_state,
        name="resource ArtifactStore verifier",
    )
    require_resource_journal_authority(phase="during target-host run")
    require_resource_platform_authority(phase="during target-host run")
    require_resource_class_authority(phase="during target-host run")
    if ArtifactStore.publish_bytes is not artifact_publish:
        raise RuntimeTargetHostResourceEvidenceError(
            "ArtifactStore publisher authority changed during target-host run"
        )
    require_callable(
        artifact_publish,
        artifact_publish_state,
        name="ArtifactStore publisher",
    )
    require_store(evidence_store, store_authority)
    require_callable(
        capture_snapshot, capture_state, name="resource snapshot collector"
    )
    require_callable(issue_evidence, issue_state, name="resource evidence issuer")
    require_callable(
        publish_evidence, publish_state, name="resource evidence publisher"
    )
    require_callable(
        read_snapshot, read_state, name="ArtifactStore authenticated reader"
    )
    require_callable(runner, runner_state, name="canonical target-host runner")

    # Freeze the durable backlog endpoint before the final resource snapshot.
    # High-water reconstruction may run afterwards, but it is only accepted if
    # no outbox transition occurred after this endpoint. This prevents a
    # post-resource-cut ENQUEUED->DELIVERED pair from inflating the retained
    # campaign high-water while restoring the same terminal pending count.
    backlog_end = outbox_backlog_tail_cut(
        journal,
        start_transition_sequence=backlog_start["transition_sequence"],
        start_pending_count=backlog_start["pending_count"],
    )

    # Preserve the inherited resource-measurement boundary: the second
    # process/disk cut closes after the caller workload, authority revalidation,
    # and the indexed durable backlog tail cut. Full backlog replay is
    # qualification work and may perform SQLite reads proportional to the
    # campaign transition count, so it remains outside elapsed/CPU/I/O metrics.
    after = capture_snapshot(evidence_root=evidence_store.root)
    backlog_evidence = outbox_backlog_high_water(
        journal,
        start_transition_sequence=backlog_start["transition_sequence"],
        start_pending_count=backlog_start["pending_count"],
    )
    if (
        backlog_evidence["end_transition_sequence"]
        != backlog_end["transition_sequence"]
        or backlog_evidence["end_pending_count"] != backlog_end["pending_count"]
    ):
        raise RuntimeTargetHostResourceEvidenceError(
            "outbox backlog changed after the post-workload causal cut"
        )
    evidence = issue_evidence(
        run,
        before=before,
        after=after,
        outbox_backlog=backlog_evidence,
        _issuer_token=_ISSUER_TOKEN,
    )
    published = publish_evidence(
        evidence_store,
        artifact_id=resource_artifact_id,
        evidence=evidence,
    )
    manifest, raw = read_snapshot(evidence_store, resource_artifact_id)
    if (
        manifest.get("sha256") != published.payload_sha256
        or raw != evidence.canonical_bytes()
    ):
        raise RuntimeTargetHostResourceEvidenceError(
            "retained target-host resource evidence changed after publication"
        )
    return RuntimeTargetHostResourceRunResult(
        run=run,
        resource_evidence=evidence,
        published_resource_evidence=published,
    )
