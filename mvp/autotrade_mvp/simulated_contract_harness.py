"""Deterministic provider-contract fault harness for AutoTrade integration tests.

This module extends the first-party simulated provider with scripted provider
outcomes and recovery evidence.  It deliberately has no networking or live
credentials and therefore cannot qualify a real venue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Literal, Mapping
from uuid import NAMESPACE_URL, uuid5

from .provider_core import ClockGuard, ProviderCoreError, QuotaBucket
from .simulated_provider import SimulatedProvider, SimulatedProviderConflict


SubmissionOutcome = Literal["ACKNOWLEDGED", "REJECTED", "UNKNOWN"]


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _instant(value: object, *, name: str) -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _decimal_text(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _evidence(kind: str, key: str, observed_at: str, payload: object) -> dict[str, str]:
    canonical_observed_at = (
        _instant(observed_at, name="observed_at")
        .isoformat()
        .replace("+00:00", "Z")
    )
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return {
        "artifact_id": str(
            uuid5(
                NAMESPACE_URL,
                f"https://sim.autotrade.local/harness/{kind}/{key}",
            )
        ),
        "sha256": "sha256:" + sha256(encoded).hexdigest(),
        "source_uri": f"https://sim.autotrade.local/harness/{kind}/{key}",
        "observed_at": canonical_observed_at,
        "rights_id": "simulated-first-party",
    }


@dataclass(frozen=True)
class SubmissionDirective:
    """Immutable instruction for one client-order identity."""

    outcome: SubmissionOutcome
    persist_unknown: bool = False
    reason_code: str = "scripted_provider_outcome"
    history_visible_at: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"ACKNOWLEDGED", "REJECTED", "UNKNOWN"}:
            raise ValueError("unsupported scripted submission outcome")
        if self.persist_unknown and self.outcome != "UNKNOWN":
            raise ValueError("persist_unknown is valid only for UNKNOWN")
        object.__setattr__(self, "reason_code", _text(self.reason_code, name="reason_code"))
        if self.history_visible_at is not None:
            _instant(self.history_visible_at, name="history_visible_at")


def _freeze_stream_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                _text(key, name="stream payload key"): _freeze_stream_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_stream_value(item) for item in value)
    if isinstance(value, float):
        raise TypeError("stream financial values must not use binary float")
    if isinstance(value, Decimal):
        return _decimal(value, name="stream decimal")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(
        "stream payload values must be immutable JSON scalars, exact Decimal, mappings or sequences"
    )


@dataclass(frozen=True)
class StreamEvent:
    sequence: int
    observed_at: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence <= 0
        ):
            raise ValueError("sequence must be a positive integer")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        canonical_time = (
            _instant(self.observed_at, name="observed_at")
            .isoformat()
            .replace("+00:00", "Z")
        )
        object.__setattr__(self, "observed_at", canonical_time)
        object.__setattr__(
            self,
            "payload",
            _freeze_stream_value(self.payload),
        )


class SimulatedProviderContractHarness:
    """Scripted failure matrix around :class:`SimulatedProvider`.

    The harness preserves the production safety distinction between a provider
    rejection and an ambiguous timeout after transport started.  UNKNOWN never
    becomes retryable merely because this deterministic harness knows whether
    the simulated venue persisted the request.
    """

    def __init__(
        self,
        provider: SimulatedProvider | None = None,
        *,
        quota_capacity: object = "100",
        recovery_quota_reserve: object = "10",
        maximum_clock_skew_seconds: int = 5,
    ) -> None:
        if (
            not isinstance(maximum_clock_skew_seconds, int)
            or isinstance(maximum_clock_skew_seconds, bool)
            or maximum_clock_skew_seconds <= 0
        ):
            raise ValueError("maximum_clock_skew_seconds must be a positive integer")
        self.provider = provider or SimulatedProvider()
        self.quota = QuotaBucket(
            capacity=_decimal(quota_capacity, name="quota_capacity"),
            recovery_reserve=_decimal(
                recovery_quota_reserve,
                name="recovery_quota_reserve",
            ),
        )
        self.clock_guard = ClockGuard(timedelta(seconds=maximum_clock_skew_seconds))
        self._submission_directives: dict[str, SubmissionDirective] = {}
        self._submission_started_at: dict[str, str] = {}
        self._stream_events: list[StreamEvent] = []
        self._corrections: dict[str, dict[str, object]] = {}

    def script_submission(
        self,
        client_order_id: str,
        directive: SubmissionDirective,
    ) -> None:
        cid = _text(client_order_id, name="client_order_id")
        if not isinstance(directive, SubmissionDirective):
            raise TypeError("directive must be a SubmissionDirective")
        previous = self._submission_directives.get(cid)
        if previous is not None and previous != directive:
            raise SimulatedProviderConflict(
                "client_order_id already has a different scripted directive"
            )
        self._submission_directives[cid] = directive

    def transport_send(
        self,
        client_order_id: str,
        request: Mapping[str, Any],
        final_guard,
        *,
        purpose: Literal["RECOVERY", "TRADING", "RESEARCH"] = "TRADING",
        quota_cost: object = "1",
    ) -> dict[str, Any]:
        """Apply quota/clock checks, then the final guard immediately before send."""

        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        cid = _text(client_order_id, name="client_order_id")
        if cid in self._submission_started_at:
            raise SimulatedProviderConflict(
                "client_order_id already crossed the transport boundary; blind retry is forbidden"
            )
        host_time = _instant(request["now"], name="now")
        provider_time = _instant(request.get("provider_now", request["now"]), name="provider_now")
        self.clock_guard.require_safe(host_time=host_time, provider_time=provider_time)
        quota_amount = _decimal(quota_cost, name="quota_cost")
        self.quota.acquire(quota_amount, purpose=purpose)

        # No transport effect is allowed before the final authority/revocation guard.
        # A rejected final guard did not consume provider quota because no request
        # crossed the transport boundary.
        try:
            final_guard()
        except BaseException:
            self.quota.release(quota_amount)
            raise
        self._submission_started_at[cid] = (
            host_time.isoformat().replace("+00:00", "Z")
        )
        self.provider.outbound_request_count += 1

        directive = self._submission_directives.get(
            cid,
            SubmissionDirective("ACKNOWLEDGED"),
        )
        if directive.outcome == "ACKNOWLEDGED":
            return self.provider.submit_order(
                attempt_id=request["attempt_id"],
                client_order_id=cid,
                instrument_version=request["instrument_version"],
                side=request["side"],
                quantity=request["quantity"],
                price=request["price"],
                now=request["now"],
                fill_immediately=request.get("fill_immediately", True),
            )

        core = {
            "attempt_id": request["attempt_id"],
            "client_order_id": cid,
            "provider_received_at": request["now"],
            "outcome": directive.outcome,
            "reason_codes": [directive.reason_code],
        }
        if directive.outcome == "REJECTED":
            return {
                **core,
                "evidence": [_evidence("rejection", cid, request["now"], core)],
                "retry_disposition": "NEVER",
                "reconciliation_required": False,
            }

        # UNKNOWN intentionally hides whether the remote side persisted the write.
        if directive.persist_unknown:
            self.provider.submit_order(
                attempt_id=request["attempt_id"],
                client_order_id=cid,
                instrument_version=request["instrument_version"],
                side=request["side"],
                quantity=request["quantity"],
                price=request["price"],
                now=request["now"],
                fill_immediately=request.get("fill_immediately", True),
            )
        return {
            **core,
            "evidence": [_evidence("unknown", cid, request["now"], core)],
            "retry_disposition": "NEVER",
            "reconciliation_required": True,
        }

    def query_order(
        self,
        *,
        client_order_id: str,
        coverage_start: str,
        coverage_end: str,
        pagination_complete: bool,
        now: str,
    ) -> dict[str, Any]:
        """Respect scripted history lag before delegating to complete coverage."""

        cid = _text(client_order_id, name="client_order_id")
        start = _instant(coverage_start, name="coverage_start")
        end = _instant(coverage_end, name="coverage_end")
        current = _instant(now, name="now")
        if end < start:
            raise ValueError("coverage_end must not precede coverage_start")
        if not isinstance(pagination_complete, bool):
            raise TypeError("pagination_complete must be boolean")
        if end > current:
            core = {
                "verdict": "INCONCLUSIVE",
                "searched_surfaces": ["orders-by-client-id", "activity-fills"],
                "time_window": {"start": coverage_start, "end": coverage_end},
                "pagination_complete": pagination_complete,
                "consistency_horizon": coverage_end,
                "reason_codes": ["coverage_end_after_query_time"],
            }
            return {
                **core,
                "evidence": [_evidence("future-coverage", cid, now, core)],
            }
        submission_started_at = self._submission_started_at.get(cid)
        if submission_started_at is not None:
            sent_at = _instant(submission_started_at, name="submission_started_at")
            if not (start <= sent_at <= end):
                core = {
                    "verdict": "INCONCLUSIVE",
                    "searched_surfaces": ["orders-by-client-id", "activity-fills"],
                    "time_window": {"start": coverage_start, "end": coverage_end},
                    "pagination_complete": pagination_complete,
                    "consistency_horizon": coverage_end,
                    "reason_codes": ["submission_outside_coverage"],
                    "submission_started_at": submission_started_at,
                }
                return {
                    **core,
                    "evidence": [_evidence("coverage-gap", cid, now, core)],
                }
        directive = self._submission_directives.get(cid)
        if (
            directive is not None
            and directive.history_visible_at is not None
            and current
            < _instant(directive.history_visible_at, name="history_visible_at")
        ):
            core = {
                "verdict": "INCONCLUSIVE",
                "searched_surfaces": ["orders-by-client-id", "activity-fills"],
                "time_window": {"start": coverage_start, "end": coverage_end},
                "pagination_complete": pagination_complete,
                "consistency_horizon": directive.history_visible_at,
                "reason_codes": ["provider_history_lag"],
            }
            return {
                **core,
                "evidence": [_evidence("history-lag", cid, now, core)],
            }
        return self.provider.query_order(
            client_order_id=cid,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            pagination_complete=pagination_complete,
            now=now,
        )

    def emit_stream_event(
        self,
        *,
        sequence: int,
        observed_at: str,
        payload: Mapping[str, object],
    ) -> StreamEvent:
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
            raise ValueError("sequence must be a positive integer")
        _instant(observed_at, name="observed_at")
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        if self._stream_events and sequence <= self._stream_events[-1].sequence:
            raise ValueError("stream sequence must advance")
        event = StreamEvent(sequence, observed_at, dict(payload))
        self._stream_events.append(event)
        return event

    def stream_after(self, after_sequence: int) -> dict[str, object]:
        if (
            not isinstance(after_sequence, int)
            or isinstance(after_sequence, bool)
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a non-negative integer")
        events = tuple(event for event in self._stream_events if event.sequence > after_sequence)
        expected = after_sequence + 1
        first = events[0].sequence if events else None
        gap_at: int | None = None
        next_expected = expected
        for event in events:
            if event.sequence != next_expected:
                gap_at = next_expected
                break
            next_expected += 1
        return {
            "after_sequence": after_sequence,
            "gap_detected": gap_at is not None,
            "expected_sequence": expected,
            "first_available_sequence": first,
            "gap_at_sequence": gap_at,
            "events": events,
        }

    def record_fee_correction(
        self,
        *,
        correction_id: str,
        provider_execution_id: str,
        fee_delta: object,
        now: str,
    ) -> Mapping[str, object]:
        """Append an idempotent correction; never mutate the original fill fact."""

        cid = _text(correction_id, name="correction_id")
        execution_id = _text(provider_execution_id, name="provider_execution_id")
        delta = _decimal(fee_delta, name="fee_delta")
        _instant(now, name="now")
        matching = [
            fill
            for fill in self.provider.activity_fills()
            if fill["provider_execution_id"] == execution_id
        ]
        if not matching:
            raise KeyError(execution_id)
        core = {
            "correction_id": cid,
            "provider_execution_id": execution_id,
            "fee_delta": _decimal_text(delta),
            "currency": self.provider.currency,
            "observed_at": now,
        }
        previous = self._corrections.get(cid)
        if previous is not None:
            comparable = {k: v for k, v in previous.items() if k != "evidence"}
            if comparable != core:
                raise SimulatedProviderConflict(
                    "correction_id already has different content"
                )
            return previous

        # Positive fee delta is an additional charge; negative is a rebate.
        self.provider.cash -= delta
        record = {
            **core,
            "evidence": [_evidence("fee-correction", cid, now, core)],
        }
        self._corrections[cid] = record
        return record

    def activity_corrections(self) -> tuple[Mapping[str, object], ...]:
        return tuple(self._corrections.values())

    def health(self, *, now: str, outage: bool = False) -> dict[str, object]:
        _instant(now, name="now")
        if not isinstance(outage, bool):
            raise TypeError("outage must be boolean")
        if not outage:
            return self.provider.health(now=now)
        return {
            "component": "SIMULATED_PROVIDER",
            "as_of": now,
            "status": "DEGRADED",
            "affected_scope": ["SIMULATION"],
            "reason_codes": ["scripted_provider_outage"],
            "last_good_at": None,
            "next_action": "reconcile_before_new_risk",
        }
