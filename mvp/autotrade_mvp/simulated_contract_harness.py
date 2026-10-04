"""Deterministic provider-contract fault harness for AutoTrade integration tests.

This module extends the first-party simulated provider with scripted provider
outcomes and recovery evidence.  It deliberately has no networking or live
credentials and therefore cannot qualify a real venue.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Literal, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    exact_add,
    exact_subtract,
    parse_bounded_exact_decimal,
)
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
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ValueError(f"{name} must be a finite decimal") from error


def _decimal_text(value: Decimal) -> str:
    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise ValueError("decimal must be finite") from error


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
        if type(self.persist_unknown) is not bool:
            raise TypeError("persist_unknown must be boolean")
        if self.persist_unknown and self.outcome != "UNKNOWN":
            raise ValueError("persist_unknown is valid only for UNKNOWN")
        object.__setattr__(self, "reason_code", _text(self.reason_code, name="reason_code"))
        if self.history_visible_at is not None:
            canonical = (
                _instant(self.history_visible_at, name="history_visible_at")
                .isoformat()
                .replace("+00:00", "Z")
            )
            object.__setattr__(self, "history_visible_at", canonical)


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


def _encode_state_value(value: object) -> dict[str, object]:
    """Encode immutable stream values without losing Decimal/scalar identity."""

    if isinstance(value, Mapping):
        return {
            "kind": "mapping",
            "items": [
                [str(key), _encode_state_value(item)]
                for key, item in sorted(value.items())
            ],
        }
    if isinstance(value, (list, tuple)):
        return {
            "kind": "sequence",
            "items": [_encode_state_value(item) for item in value],
        }
    if isinstance(value, Decimal):
        return {"kind": "decimal", "value": _decimal_text(value)}
    if value is None or isinstance(value, (str, int, bool)):
        return {"kind": "scalar", "value": value}
    raise TypeError("unsupported immutable state value")


def _decode_state_value(value: object) -> object:
    if not isinstance(value, Mapping):
        raise ValueError("encoded state value must be a mapping")
    kind = value.get("kind")
    if kind == "mapping":
        if set(value) != {"kind", "items"}:
            raise ValueError("encoded mapping state has unsupported or missing fields")
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("encoded mapping items must be a list")
        result: dict[str, object] = {}
        for pair in items:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not pair[0]
            ):
                raise ValueError("encoded mapping item is malformed")
            if pair[0] in result:
                raise ValueError("encoded mapping contains duplicate key")
            result[pair[0]] = _decode_state_value(pair[1])
        return result
    if kind == "sequence":
        if set(value) != {"kind", "items"}:
            raise ValueError("encoded sequence state has unsupported or missing fields")
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("encoded sequence items must be a list")
        return tuple(_decode_state_value(item) for item in items)
    if kind == "decimal":
        if set(value) != {"kind", "value"}:
            raise ValueError("encoded decimal state has unsupported or missing fields")
        return _decimal(value.get("value"), name="encoded decimal")
    if kind == "scalar":
        if set(value) != {"kind", "value"}:
            raise ValueError("encoded scalar state has unsupported or missing fields")
        scalar = value.get("value")
        if scalar is not None and not isinstance(scalar, (str, int, bool)):
            raise ValueError("encoded scalar has unsupported type")
        return scalar
    raise ValueError("encoded state value kind is unsupported")


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

    def export_state(self) -> dict[str, object]:
        with self.provider._mutation_lock:
            return self._export_state_unlocked()

    def _export_state_unlocked(self) -> dict[str, object]:
        """Export restartable scripted-provider and fault-harness truth."""

        provider_base_state = self.provider.export_state()
        provider_base_cash = _decimal(
            provider_base_state["cash"],
            name="provider cash",
        )
        # Undo accepted fee corrections in reverse application order. Each
        # intermediate is therefore a cash state that previously passed the
        # shared bounded exact-decimal authority. Summing all deltas first is
        # not equivalent: the aggregate itself can exceed the envelope even
        # when every live transition and the final state are valid.
        for record in reversed(tuple(self._corrections.values())):
            provider_base_cash = exact_add(
                provider_base_cash,
                _decimal(record["fee_delta"], name="fee_delta"),
            )
        provider_base_state["cash"] = _decimal_text(provider_base_cash)
        provider_base_body = {
            key: value
            for key, value in provider_base_state.items()
            if key != "state_digest"
        }
        provider_base_state["state_digest"] = (
            "sha256:"
            + sha256(
                json.dumps(
                    provider_base_body,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        )

        body: dict[str, object] = {
            "schema_version": "simulated-provider-harness-state-v1",
            "provider_base_state": provider_base_state,
            "quota": {
                "capacity": _decimal_text(self.quota.capacity),
                "recovery_reserve": _decimal_text(self.quota.recovery_reserve),
                "used": _decimal_text(self.quota.used),
            },
            "maximum_clock_skew_seconds": int(
                self.clock_guard.maximum_absolute_skew.total_seconds()
            ),
            "submission_directives": {
                cid: {
                    "outcome": directive.outcome,
                    "persist_unknown": directive.persist_unknown,
                    "reason_code": directive.reason_code,
                    "history_visible_at": directive.history_visible_at,
                }
                for cid, directive in sorted(self._submission_directives.items())
            },
            "submission_started_at": dict(
                sorted(self._submission_started_at.items())
            ),
            "stream_events": [
                {
                    "sequence": event.sequence,
                    "observed_at": event.observed_at,
                    "payload": _encode_state_value(event.payload),
                }
                for event in self._stream_events
            ],
            # Correction list order is durable application chronology. Exact bounded
            # arithmetic is not permutation-safe at the resource envelope, so
            # restart must replay the same sequence that mutated provider cash.
            "corrections": [
                deepcopy(record)
                for record in self._corrections.values()
            ],
        }
        encoded = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return {
            **body,
            "state_digest": "sha256:" + sha256(encoded).hexdigest(),
        }

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, object],
    ) -> "SimulatedProviderContractHarness":
        """Restore the scripted harness without weakening UNKNOWN/retry semantics."""

        if not isinstance(state, Mapping):
            raise TypeError("state must be a mapping")
        raw = dict(state)
        digest = _text(raw.pop("state_digest", None), name="state_digest")
        encoded = json.dumps(
            raw,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if digest != "sha256:" + sha256(encoded).hexdigest():
            raise SimulatedProviderConflict("simulated harness state digest mismatch")
        if raw.get("schema_version") != "simulated-provider-harness-state-v1":
            raise ValueError("unsupported simulated harness state schema")
        expected_state_fields = {
            "schema_version",
            "provider_base_state",
            "quota",
            "maximum_clock_skew_seconds",
            "submission_directives",
            "submission_started_at",
            "stream_events",
            "corrections",
        }
        if set(raw) != expected_state_fields:
            raise SimulatedProviderConflict(
                "simulated harness state has unsupported or missing fields"
            )

        quota = raw.get("quota")
        if not isinstance(quota, Mapping):
            raise ValueError("quota state is required")
        if set(quota) != {"capacity", "recovery_reserve", "used"}:
            raise SimulatedProviderConflict(
                "quota state has unsupported or missing fields"
            )
        maximum_clock_skew_seconds = raw.get("maximum_clock_skew_seconds")
        if (
            not isinstance(maximum_clock_skew_seconds, int)
            or isinstance(maximum_clock_skew_seconds, bool)
            or maximum_clock_skew_seconds <= 0
        ):
            raise ValueError("maximum_clock_skew_seconds must be positive")

        provider_state = raw.get("provider_base_state")
        if not isinstance(provider_state, Mapping):
            raise ValueError("provider_base_state is required")
        provider = SimulatedProvider.from_state(provider_state)
        harness = cls(
            provider,
            quota_capacity=quota.get("capacity"),
            recovery_quota_reserve=quota.get("recovery_reserve"),
            maximum_clock_skew_seconds=maximum_clock_skew_seconds,
        )
        used = _decimal(quota.get("used"), name="quota used")
        harness.quota = QuotaBucket(
            capacity=harness.quota.capacity,
            recovery_reserve=harness.quota.recovery_reserve,
            used=used,
        )

        directives = raw.get("submission_directives")
        started_at = raw.get("submission_started_at")
        stream_events = raw.get("stream_events")
        corrections = raw.get("corrections")
        if not isinstance(directives, Mapping):
            raise ValueError("submission_directives must be a mapping")
        if not isinstance(started_at, Mapping):
            raise ValueError("submission_started_at must be a mapping")
        if not isinstance(stream_events, list):
            raise ValueError("stream_events must be a list")
        if not isinstance(corrections, list):
            raise ValueError("corrections must be a list")

        directive_fields = {
            "outcome",
            "persist_unknown",
            "reason_code",
            "history_visible_at",
        }
        for cid, item in directives.items():
            if not isinstance(item, Mapping):
                raise ValueError("submission directive state must be a mapping")
            if set(item) != directive_fields:
                raise SimulatedProviderConflict(
                    "submission directive has unsupported or missing fields"
                )
            harness.script_submission(
                _text(cid, name="client_order_id"),
                SubmissionDirective(
                    item.get("outcome"),
                    persist_unknown=item.get("persist_unknown", False),
                    reason_code=item.get("reason_code"),
                    history_visible_at=item.get("history_visible_at"),
                ),
            )

        for cid, observed_at in started_at.items():
            client_order_id = _text(cid, name="client_order_id")
            if client_order_id in harness._submission_started_at:
                raise SimulatedProviderConflict(
                    "duplicate submission-start identity in state"
                )
            canonical_started_at = (
                _instant(observed_at, name="submission_started_at")
                .isoformat()
                .replace("+00:00", "Z")
            )
            provider_order = harness.provider.orders.get(client_order_id)
            directive = harness._submission_directives.get(client_order_id)
            if provider_order is not None:
                if provider_order.submitted_at != canonical_started_at:
                    raise SimulatedProviderConflict(
                        "submission-start time does not match provider order truth"
                    )
            elif directive is None or directive.outcome == "ACKNOWLEDGED" or (
                directive.outcome == "UNKNOWN" and directive.persist_unknown
            ):
                raise SimulatedProviderConflict(
                    "submission-start identity has no matching provider or non-persisted outcome truth"
                )
            harness._submission_started_at[client_order_id] = canonical_started_at

        for item in stream_events:
            if not isinstance(item, Mapping):
                raise ValueError("stream event state must be a mapping")
            if set(item) != {"sequence", "observed_at", "payload"}:
                raise SimulatedProviderConflict(
                    "stream event state has unsupported or missing fields"
                )
            payload = _decode_state_value(item.get("payload"))
            if not isinstance(payload, Mapping):
                raise ValueError("stream event payload must decode to a mapping")
            harness.emit_stream_event(
                sequence=item.get("sequence"),
                observed_at=item.get("observed_at"),
                payload=payload,
            )

        last_correction_observed_at: datetime | None = None
        for item in corrections:
            if not isinstance(item, Mapping):
                raise ValueError("correction state must be a mapping")
            correction_id = _text(
                item.get("correction_id"),
                name="correction_id",
            )
            if correction_id in harness._corrections:
                raise SimulatedProviderConflict(
                    "duplicate correction identity in state"
                )
            execution_id = _text(
                item.get("provider_execution_id"),
                name="provider_execution_id",
            )
            matching = [
                fill
                for fill in harness.provider.activity_fills()
                if fill["provider_execution_id"] == execution_id
            ]
            if len(matching) != 1:
                raise SimulatedProviderConflict(
                    "correction references unknown provider execution"
                )
            delta = _decimal(item.get("fee_delta"), name="fee_delta")
            observed_instant = _instant(
                item.get("observed_at"),
                name="observed_at",
            )
            observed_at = observed_instant.isoformat().replace("+00:00", "Z")
            if observed_instant < _instant(
                matching[0].get("receipt_time"),
                name="fill receipt_time",
            ):
                raise SimulatedProviderConflict(
                    "fee correction cannot precede referenced fill"
                )
            if (
                last_correction_observed_at is not None
                and observed_instant < last_correction_observed_at
            ):
                raise SimulatedProviderConflict(
                    "fee correction observation time must not move backwards"
                )
            core = {
                "correction_id": correction_id,
                "provider_execution_id": execution_id,
                "fee_delta": _decimal_text(delta),
                "currency": harness.provider.currency,
                "observed_at": observed_at,
            }
            if item.get("currency") != harness.provider.currency:
                raise SimulatedProviderConflict(
                    "correction currency does not match provider account"
                )
            expected = {
                **core,
                "evidence": [
                    _evidence(
                        "fee-correction",
                        correction_id,
                        observed_at,
                        core,
                    )
                ],
            }
            if dict(item) != expected:
                raise SimulatedProviderConflict(
                    "serialized correction does not match canonical evidence"
                )
            new_cash = exact_subtract(harness.provider.cash, delta)
            harness.provider.cash = new_cash
            harness._corrections[correction_id] = expected
            last_correction_observed_at = observed_instant

        return harness

    def script_submission(
        self,
        client_order_id: str,
        directive: SubmissionDirective,
    ) -> None:
        with self.provider._mutation_lock:
            self.provider._require_provider_mutation_allowed()
            self._script_submission_unlocked(client_order_id, directive)

    def _script_submission_unlocked(
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
        # Validate the complete provider request before the final send barrier.
        # A local shape/value error is provably NOT_SENT and must never consume
        # outbound identity, quota, or become an ambiguous provider write.
        attempt_id = _text(request.get("attempt_id"), name="attempt_id")
        try:
            UUID(attempt_id)
        except ValueError as error:
            raise ValueError("attempt_id must be a UUID") from error
        instrument_version = _text(
            request.get("instrument_version"),
            name="instrument_version",
        )
        side = _text(request.get("side"), name="side").upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        quantity = _decimal(request.get("quantity"), name="quantity")
        price = _decimal(request.get("price"), name="price")
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be positive")

        host_time = _instant(request.get("now"), name="now")
        provider_time = _instant(
            request.get("provider_now", request.get("now")),
            name="provider_now",
        )
        self.clock_guard.require_safe(host_time=host_time, provider_time=provider_time)
        fill_immediately = request.get("fill_immediately", True)
        if type(fill_immediately) is not bool:
            raise TypeError("fill_immediately must be boolean")
        canonical_now = host_time.isoformat().replace("+00:00", "Z")

        # Canonical request shape/clock checks above apply to every scripted
        # transport outcome. Provider persistence/economic admissibility is a
        # stronger local fact and is required only when this scripted outcome
        # can actually persist through provider.submit_order(). REJECTED and
        # non-persisted UNKNOWN deliberately model a remote outcome without
        # mutating provider order/cash/position truth.
        with self.provider._transport_commit_fence():
            # The fast check above is diagnostic only. Two callers can pass it
            # before either acquires the provider fence; admit exactly one here.
            if cid in self._submission_started_at:
                raise SimulatedProviderConflict(
                    "client_order_id already crossed the transport boundary; blind retry is forbidden"
                )
            directive = self._submission_directives.get(
                cid,
                SubmissionDirective("ACKNOWLEDGED"),
            )
            prepared = None
            if directive.outcome == "ACKNOWLEDGED" or (
                directive.outcome == "UNKNOWN" and directive.persist_unknown
            ):
                prepared = self.provider._prepare_submission(
                    attempt_id=attempt_id,
                    client_order_id=cid,
                    instrument_version=instrument_version,
                    side=side,
                    quantity=quantity,
                    price=price,
                    now=canonical_now,
                    fill_immediately=fill_immediately,
                )

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
            self._submission_started_at[cid] = canonical_now
            self.provider.outbound_request_count += 1

            if directive.outcome == "ACKNOWLEDGED":
                return self.provider._commit_prepared_submission(prepared)

            core = {
                "attempt_id": attempt_id,
                "client_order_id": cid,
                "provider_received_at": canonical_now,
                "outcome": directive.outcome,
                "reason_codes": [directive.reason_code],
            }
            if directive.outcome == "REJECTED":
                return {
                    **core,
                    "evidence": [_evidence("rejection", cid, canonical_now, core)],
                    "retry_disposition": "NEVER",
                    "reconciliation_required": False,
                }

            # UNKNOWN intentionally hides whether the remote side persisted the write.
            if directive.persist_unknown:
                self.provider._commit_prepared_submission(prepared)
            return {
                **core,
                "evidence": [_evidence("unknown", cid, canonical_now, core)],
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
        horizon = (current if end > current else end).isoformat().replace(
            "+00:00", "Z"
        )
        if cid not in self.provider.orders and end > current:
            core = {
                "verdict": "INCONCLUSIVE",
                "searched_surfaces": ["orders-by-client-id", "activity-fills"],
                "time_window": {"start": coverage_start, "end": coverage_end},
                "pagination_complete": pagination_complete,
                "consistency_horizon": horizon,
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
                    "consistency_horizon": horizon,
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
        with self.provider._mutation_lock:
            self.provider._require_provider_mutation_allowed()
            return self._emit_stream_event_unlocked(
                sequence=sequence, observed_at=observed_at, payload=payload,
            )

    def _emit_stream_event_unlocked(
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
        if self._stream_events and _instant(
            event.observed_at,
            name="observed_at",
        ) < _instant(
            self._stream_events[-1].observed_at,
            name="previous observed_at",
        ):
            raise SimulatedProviderConflict(
                "stream observation time must not move backwards"
            )
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
        """Append an idempotent correction without racing a prepared send."""

        with self.provider._mutation_lock:
            self.provider._require_provider_mutation_allowed()
            return self._record_fee_correction_unlocked(
                correction_id=correction_id,
                provider_execution_id=provider_execution_id,
                fee_delta=fee_delta,
                now=now,
            )

    def _record_fee_correction_unlocked(
        self,
        *,
        correction_id: str,
        provider_execution_id: str,
        fee_delta: object,
        now: str,
    ) -> Mapping[str, object]:
        """Canonical correction implementation under the provider mutation lock."""

        cid = _text(correction_id, name="correction_id")
        execution_id = _text(provider_execution_id, name="provider_execution_id")
        delta = _decimal(fee_delta, name="fee_delta")
        canonical_instant = _instant(now, name="now")
        canonical_now = canonical_instant.isoformat().replace("+00:00", "Z")
        matching = [
            fill
            for fill in self.provider.activity_fills()
            if fill["provider_execution_id"] == execution_id
        ]
        if not matching:
            raise KeyError(execution_id)
        if canonical_instant < _instant(
            matching[0].get("receipt_time"),
            name="fill receipt_time",
        ):
            raise SimulatedProviderConflict(
                "fee correction cannot precede referenced fill"
            )
        core = {
            "correction_id": cid,
            "provider_execution_id": execution_id,
            "fee_delta": _decimal_text(delta),
            "currency": self.provider.currency,
            "observed_at": canonical_now,
        }
        previous = self._corrections.get(cid)
        if previous is not None:
            comparable = {k: v for k, v in previous.items() if k != "evidence"}
            if comparable != core:
                raise SimulatedProviderConflict(
                    "correction_id already has different content"
                )
            return deepcopy(previous)

        if self._corrections:
            last_record = next(reversed(self._corrections.values()))
            last_observed_at = _instant(
                last_record.get("observed_at"),
                name="previous correction observed_at",
            )
            if canonical_instant < last_observed_at:
                raise SimulatedProviderConflict(
                    "fee correction observation time must not move backwards"
                )

        # Positive fee delta is an additional charge; negative is a rebate.
        # Compute the complete exact cash successor before mutating either the
        # provider balance or correction registry.
        new_cash = exact_subtract(self.provider.cash, delta)
        record = {
            **core,
            "evidence": [_evidence("fee-correction", cid, canonical_now, core)],
        }
        self.provider.cash = new_cash
        self._corrections[cid] = record
        return deepcopy(record)

    def activity_corrections(self) -> tuple[Mapping[str, object], ...]:
        return tuple(deepcopy(record) for record in self._corrections.values())

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
