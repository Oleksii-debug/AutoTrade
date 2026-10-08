"""Read-only, credential-free operator projection of existing AutoTrade truth.

This module does not mint recovery, journal, market-data, model, financial or
order authority. Input must come from an authenticated canonical Host snapshot
and the selected RecoveryController / RuntimeSafetySignals. It emits only
strict, bounded counts and status labels; no caller-provided payload is logged.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .diagnostics import DiagnosticSnapshot
from .host_network import _SNAPSHOT_FIELDS
from .readiness import RuntimeSafetySignals, evaluate_readiness
from .recovery import HostState, RecoveryController


_DOMAINS = (
    "health", "readiness", "recovery", "risk", "reservations",
    "orders", "unknown", "portfolio", "jobs", "model", "evidence",
)
_MAX_ITEMS = 10_000
_PUBLIC_CODE_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-."
)
_KNOWN_REASON_CODES = frozenset({
    "SIMULATION_ONLY", "ECONOMIC_EDGE_UNPROVEN",
    "clock_requalification_required", "clock_untrusted",
    "durable_journal_unavailable", "lease_expired_no_failover",
    "legacy_submission_identity_unrecoverable", "provider_uncertainty",
    "startup_reconciliation_required", "stopped",
    "clock_skew_exceeded", "emergency_disk_reserve_unavailable",
    "emergency_execution_path_unqualified", "external_uncertainty_unresolved",
    "journal_not_writable", "market_data_stale",
    "new_exposure_protection_path_unqualified", "old_sender_not_fenced",
    "provider_native_protection_absent", "provider_not_authenticated",
    "provider_reconciliation_incomplete", "reconciliation_lag_exceeded",
    "recovery_in_progress", "schema_incompatible",
    "sender_ownership_unproven", "unknown_sends_present",
})


def _public_code(value: object, *, name: str) -> str:
    """Admit bounded machine codes only, never free-text/provider messages."""
    if (
        type(value) is not str
        or not 1 <= len(value) <= 64
        or value[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        or any(ch not in _PUBLIC_CODE_CHARACTERS for ch in value)
    ):
        raise ValueError(f"{name} must be an exact nonsecret bounded status code")
    # Do not reflect unknown caller-supplied values, even when they resemble
    # valid machine codes: a bearer key can be alphanumeric without markers.
    return value if value in _KNOWN_REASON_CODES else "unrecognized_reason_code"


def _plain_map(value: object, *, name: str) -> dict:
    # Only the exact mappingproxy emitted by AuthenticatedHostApplication
    # or an exact JSON dict is admissible. A user Mapping subclass can execute
    # attacker-defined items/get/iter methods before any masking.
    if type(value) is dict:
        return dict(value)
    if type(value) is MappingProxyType:
        return dict(value)
    raise TypeError(f"{name} must be an exact Host mapping")


def _count_array(mapping: dict, field: str, *, name: str) -> int | None:
    if field not in mapping:
        return None
    value = mapping[field]
    if type(value) is not list:
        raise ValueError(f"{name} must be an exact array")
    if len(value) > _MAX_ITEMS:
        raise ValueError(f"{name} exceeds the bounded diagnostic item budget")
    # Deliberately never materialize elements or their IDs/content.
    return len(value)


def _codes(value: object, *, name: str) -> tuple[str, ...]:
    if type(value) not in (list, tuple, set, frozenset):
        raise TypeError(f"{name} must be a plain sequence of reason codes")
    if len(value) > 128:
        raise ValueError(f"{name} has too many reason codes")
    result = tuple(_public_code(item, name=name) for item in value)
    return tuple(sorted(set(result)))


@dataclass(frozen=True, slots=True)
class OperatorObservabilitySnapshot:
    """Non-authoritative, screen-reader-linear operator read model."""

    mode: str
    reasons: tuple[str, ...]
    domains: MappingProxyType

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "reason_codes": list(self.reasons),
            "financial_authority": "NONE",
            "domains": {name: dict(self.domains[name]) for name in _DOMAINS},
        }

    def to_text(self) -> str:
        lines = [
            "AutoTrade operator diagnostics",
            f"Mode: {self.mode}",
            "Financial authority: NONE",
            "Reason codes: " + (", ".join(self.reasons) if self.reasons else "none"),
        ]
        for name in _DOMAINS:
            fields = self.domains[name]
            rendered = "; ".join(
                f"{key}={str(value).lower() if type(value) is bool else ('unavailable' if value is None else value)}"
                for key, value in fields.items()
            )
            lines.append(f"{name}: {rendered}")
        return "\n".join(lines) + "\n"


def build_operator_observability(
    *,
    ui_snapshot: object,
    recovery: RecoveryController,
    signals: RuntimeSafetySignals,
    evidence: DiagnosticSnapshot | None = None,
) -> OperatorObservabilitySnapshot:
    """Project authenticated Host state plus canonical readiness/recovery.

    Callers must supply their already-validated Host UiSnapshot. This reader
    never authenticates a session, grants READY or changes a journal on its
    own. No PASS or source-reliability claim is derived from metric presence.
    """
    if type(recovery) is not RecoveryController:
        raise TypeError("recovery must be the canonical RecoveryController")
    if type(signals) is not RuntimeSafetySignals:
        raise TypeError("signals must be canonical RuntimeSafetySignals")
    if evidence is not None and type(evidence) is not DiagnosticSnapshot:
        raise TypeError("evidence must be the canonical DiagnosticSnapshot")
    snapshot = _plain_map(ui_snapshot, name="UiSnapshot")
    if frozenset(snapshot) != _SNAPSHOT_FIELDS:
        raise ValueError("UiSnapshot fields do not match canonical Host contract")
    for name in ("portfolio", "risk", "strategy", "connection_freshness"):
        snapshot[name] = _plain_map(snapshot[name], name=name)
    if type(snapshot["jobs"]) is not list or len(snapshot["jobs"]) > _MAX_ITEMS:
        raise ValueError("Host jobs must be a bounded exact array")
    if not snapshot["connection_freshness"]:
        raise ValueError("Host freshness projection is missing")
    ui_reasons = _codes(snapshot["reason_codes"], name="UiSnapshot reason_codes")
    state_before = recovery.state
    if type(state_before) is not HostState:
        raise ValueError("recovery state is not a canonical HostState")
    if type(recovery.reason_codes) is not set or type(recovery.unresolved_attempts) is not set:
        raise ValueError("recovery diagnostic source is noncanonical")
    reason_codes_before = frozenset(recovery.reason_codes)
    unresolved_before = len(recovery.unresolved_attempts)
    recovery_reasons = _codes(reason_codes_before, name="recovery reason_codes")
    readiness = evaluate_readiness(signals)
    readiness_reasons = _codes(readiness.blockers, name="readiness blockers")
    warnings = _codes(readiness.warnings, name="readiness warnings")
    mode = {
        HostState.STOPPED: "BLOCKED",
        HostState.BLOCKED: "BLOCKED",
        HostState.RECOVERING: "RECOVERING",
        HostState.DEGRADED: "DEGRADED",
        HostState.READY: ("READY" if readiness.ready and not ui_reasons else "DEGRADED"),
    }[recovery.state]
    reasons = set(ui_reasons + recovery_reasons + readiness_reasons)
    if recovery.state is HostState.STOPPED:
        reasons.add("host_stopped")
    if recovery.state is HostState.READY and mode != "READY":
        reasons.add("readiness_not_proven")
    risk = snapshot["risk"]
    portfolio = snapshot["portfolio"]
    strategy = snapshot["strategy"]
    orders = _count_array(portfolio, "orders", name="portfolio.orders")
    fills = _count_array(portfolio, "fills", name="portfolio.fills")
    reservations = _count_array(risk, "active_reservations", name="risk.active_reservations")
    # Do not expose raw portfolio, risk, order, reservation or model payloads:
    # unclassified free text can contain unrecognizable secrets and PII.
    observations = {
        "health": {"host_api": "SNAPSHOT_AVAILABLE", "financial_ready": mode == "READY"},
        "readiness": {
            "status": readiness.mode.value,
            "new_exposure_allowed": readiness.ready_for_new_exposure and mode == "READY",
            "protection_only_available": readiness.protection_only_available,
            "warning_count": len(warnings),
        },
        "recovery": {
            "status": recovery.state.value,
            "unresolved_attempt_count": unresolved_before,
        },
        "risk": {"status": "SOURCE_PRESENT", "financial_mode": "READ_ONLY"},
        "reservations": {"count": reservations, "source": "HOST_RISK" if reservations is not None else "UNAVAILABLE"},
        "orders": {"count": orders, "source": "HOST_PORTFOLIO" if orders is not None else "UNAVAILABLE"},
        "unknown": {"send_count": signals.unknown_send_count, "unresolved_external": signals.unresolved_external_uncertainty},
        "portfolio": {"source": "HOST_PORTFOLIO", "fill_count": fills},
        "jobs": {"count": len(snapshot["jobs"]), "source": "HOST_JOBS"},
        "model": {"source": "HOST_STRATEGY" if "model" in strategy else "UNAVAILABLE", "qualification": "NOT_ESTABLISHED"},
        "evidence": {
            "source": "SIMULATION_TRACE" if evidence is not None else "UNAVAILABLE",
            "record_count": evidence.evidence_count if evidence is not None else None,
            "science_pass": False,
        },
    }
    if (
        recovery.state is not state_before
        or frozenset(recovery.reason_codes) != reason_codes_before
        or len(recovery.unresolved_attempts) != unresolved_before
    ):
        # A diagnostic read cannot silently bridge a recovery transition.
        # A later read may produce a new projection; no financial retry occurs.
        raise ValueError("recovery state changed during diagnostic read")
    return OperatorObservabilitySnapshot(
        mode=mode,
        reasons=tuple(sorted(reasons)),
        domains=MappingProxyType({
            name: MappingProxyType(observations[name]) for name in _DOMAINS
        }),
    )
