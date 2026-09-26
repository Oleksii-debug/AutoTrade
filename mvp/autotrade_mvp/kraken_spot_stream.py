"""Fail-closed Kraken Spot WebSocket v2 executions recovery foundation.

This module deliberately owns provider-specific frame parsing and sequence/reconnect
state only. It does not own socket I/O, authentication tokens, retry policy,
reconciliation decisions, or trading readiness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Mapping, Sequence


class KrakenSpotStreamError(ValueError):
    """Raised when Kraken stream evidence cannot be used safely."""


KRAKEN_SPOT_EXECUTIONS_SUBSCRIPTION: Mapping[str, object] = MappingProxyType(
    {
        "channel": "executions",
        "snap_orders": True,
        "snap_trades": False,
        "order_status": True,
    }
)

_ALLOWED_FRAME_TYPES = frozenset({"snapshot", "update"})
_ALLOWED_EXEC_TYPES = frozenset(
    {
        "pending_new",
        "new",
        "trade",
        "filled",
        "iceberg_refill",
        "canceled",
        "expired",
        "amended",
        "restated",
        "status",
    }
)
_ALLOWED_ORDER_STATUSES = frozenset(
    {
        "pending_new",
        "new",
        "partially_filled",
        "filled",
        "canceled",
        "expired",
    }
)
_TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "expired"})
_MAX_FRAME_BYTES = 4 * 1024 * 1024


def _canonical_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise KrakenSpotStreamError(f"{name} must be non-empty text")
    if value != value.strip():
        raise KrakenSpotStreamError(f"{name} must be canonical text")
    if any(ord(character) < 0x20 for character in value):
        raise KrakenSpotStreamError(f"{name} contains control characters")
    return value


def _json_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise KrakenSpotStreamError(
                f"duplicate JSON object field: {key}"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise KrakenSpotStreamError(
        f"non-finite JSON numeric constant is forbidden: {value}"
    )


def _decode_exact_json(response_bytes: object) -> Mapping[str, object]:
    if type(response_bytes) is not bytes:
        raise KrakenSpotStreamError(
            "Kraken stream frame must be exact bytes"
        )
    if not response_bytes:
        raise KrakenSpotStreamError("Kraken stream frame is empty")
    if len(response_bytes) > _MAX_FRAME_BYTES:
        raise KrakenSpotStreamError("Kraken stream frame is too large")
    try:
        text = response_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise KrakenSpotStreamError(
            "Kraken stream frame must be UTF-8"
        ) from error
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_json_constant,
        )
    except KrakenSpotStreamError:
        raise
    except (json.JSONDecodeError, ValueError, TypeError) as error:
        raise KrakenSpotStreamError(
            "Kraken stream frame is invalid JSON"
        ) from error
    if not isinstance(decoded, Mapping):
        raise KrakenSpotStreamError(
            "Kraken stream frame root must be an object"
        )
    return decoded


@dataclass(frozen=True)
class KrakenSpotExecutionReport:
    """Minimal identity/status projection from one exact executions report."""

    order_id: str
    exec_type: str
    order_status: str | None
    client_order_id: str | None = None
    exec_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "order_id",
            _canonical_text(self.order_id, name="order_id"),
        )
        exec_type = _canonical_text(self.exec_type, name="exec_type")
        if exec_type not in _ALLOWED_EXEC_TYPES:
            raise KrakenSpotStreamError(
                "Kraken execution report exec_type is unsupported"
            )
        object.__setattr__(self, "exec_type", exec_type)

        status = self.order_status
        if status is not None:
            status = _canonical_text(status, name="order_status")
            if status not in _ALLOWED_ORDER_STATUSES:
                raise KrakenSpotStreamError(
                    "Kraken execution report order_status is unsupported"
                )
            object.__setattr__(self, "order_status", status)

        for field_name in ("client_order_id", "exec_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _canonical_text(value, name=field_name),
                )


@dataclass(frozen=True)
class KrakenSpotExecutionFrame:
    """One exact Kraken executions snapshot/update bound to account scope."""

    account_id: str
    environment: str
    frame_type: str
    sequence: int
    reports: tuple[KrakenSpotExecutionReport, ...]
    evidence_ref: str
    response_bytes: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "account_id",
            _canonical_text(self.account_id, name="account_id"),
        )
        environment = _canonical_text(
            self.environment,
            name="environment",
        ).upper()
        if environment != "LIVE":
            raise KrakenSpotStreamError(
                "Kraken Spot stream foundation permits LIVE only"
            )
        object.__setattr__(self, "environment", environment)

        frame_type = _canonical_text(
            self.frame_type,
            name="frame_type",
        )
        if frame_type not in _ALLOWED_FRAME_TYPES:
            raise KrakenSpotStreamError(
                "Kraken executions frame type must be snapshot or update"
            )
        object.__setattr__(self, "frame_type", frame_type)

        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise KrakenSpotStreamError(
                "Kraken executions sequence must be a non-negative integer"
            )
        if not isinstance(self.reports, tuple):
            raise TypeError("reports must be a tuple")
        if any(
            not isinstance(report, KrakenSpotExecutionReport)
            for report in self.reports
        ):
            raise TypeError(
                "reports must contain KrakenSpotExecutionReport"
            )
        evidence_ref = _canonical_text(
            self.evidence_ref,
            name="evidence_ref",
        )
        expected_ref = (
            "provider-stream:sha256:"
            + sha256(self.response_bytes).hexdigest()
        )
        if evidence_ref != expected_ref:
            raise KrakenSpotStreamError(
                "Kraken stream evidence_ref does not match exact bytes"
            )
        if type(self.response_bytes) is not bytes:
            raise TypeError("response_bytes must be bytes")


def parse_execution_frame(
    response_bytes: object,
    *,
    account_id: str,
    environment: str = "LIVE",
) -> KrakenSpotExecutionFrame:
    """Parse one exact WebSocket v2 executions frame without socket authority."""

    raw = _decode_exact_json(response_bytes)
    expected_fields = {"channel", "type", "data", "sequence"}
    if set(raw) != expected_fields:
        raise KrakenSpotStreamError(
            "Kraken executions frame fields are not canonical"
        )
    if raw.get("channel") != "executions":
        raise KrakenSpotStreamError(
            "Kraken stream frame channel must be executions"
        )

    frame_type = raw.get("type")
    if frame_type not in _ALLOWED_FRAME_TYPES:
        raise KrakenSpotStreamError(
            "Kraken executions frame type must be snapshot or update"
        )
    sequence = raw.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise KrakenSpotStreamError(
            "Kraken executions sequence must be a non-negative integer"
        )

    data = raw.get("data")
    if not isinstance(data, list):
        raise KrakenSpotStreamError(
            "Kraken executions data must be an array"
        )

    reports: list[KrakenSpotExecutionReport] = []
    for index, raw_report in enumerate(data):
        if not isinstance(raw_report, Mapping):
            raise KrakenSpotStreamError(
                f"Kraken executions data[{index}] must be an object"
            )
        order_id = raw_report.get("order_id")
        exec_type = raw_report.get("exec_type")
        if order_id is None:
            raise KrakenSpotStreamError(
                f"Kraken executions data[{index}] lacks order_id"
            )
        if exec_type is None:
            raise KrakenSpotStreamError(
                f"Kraken executions data[{index}] lacks exec_type"
            )
        reports.append(
            KrakenSpotExecutionReport(
                order_id=order_id,
                exec_type=exec_type,
                order_status=raw_report.get("order_status"),
                client_order_id=raw_report.get("cl_ord_id"),
                exec_id=raw_report.get("exec_id"),
            )
        )

    exact = response_bytes
    return KrakenSpotExecutionFrame(
        account_id=account_id,
        environment=environment,
        frame_type=frame_type,
        sequence=sequence,
        reports=tuple(reports),
        evidence_ref=(
            "provider-stream:sha256:" + sha256(exact).hexdigest()
        ),
        response_bytes=exact,
    )


@dataclass(frozen=True)
class KrakenSpotStreamRecoveryEvidence:
    """Non-authoritative handoff to the canonical reconciliation coordinator."""

    account_id: str
    environment: str
    connection_generation: int
    phase: str
    snapshot_sequence: int | None
    last_sequence: int | None
    snapshot_evidence_ref: str | None
    buffered_update_evidence_refs: tuple[str, ...]
    provisional_snapshot_order_ids: tuple[str, ...]
    gap_expected_sequence: int | None = None
    gap_observed_sequence: int | None = None
    gap_evidence_ref: str | None = None

    @property
    def trading_ready(self) -> bool:
        """This provider-local foundation never grants trading readiness."""

        return False


class KrakenSpotExecutionStreamRecovery:
    """Detect snapshot/reconnect/sequence gaps without inventing READY state."""

    DISCONNECTED = "DISCONNECTED"
    AWAITING_SNAPSHOT = "AWAITING_SNAPSHOT"
    REST_RECONCILIATION_REQUIRED = "REST_RECONCILIATION_REQUIRED"
    GAP_RECONCILIATION_REQUIRED = "GAP_RECONCILIATION_REQUIRED"

    def __init__(
        self,
        *,
        account_id: str,
        environment: str = "LIVE",
        max_buffered_updates: int = 1000,
    ) -> None:
        self.account_id = _canonical_text(
            account_id,
            name="account_id",
        )
        normalized_environment = _canonical_text(
            environment,
            name="environment",
        ).upper()
        if normalized_environment != "LIVE":
            raise KrakenSpotStreamError(
                "Kraken Spot stream recovery permits LIVE only"
            )
        if (
            isinstance(max_buffered_updates, bool)
            or not isinstance(max_buffered_updates, int)
            or max_buffered_updates < 1
            or max_buffered_updates > 100_000
        ):
            raise KrakenSpotStreamError(
                "max_buffered_updates must be an integer in 1..100000"
            )
        self.environment = normalized_environment
        self.max_buffered_updates = max_buffered_updates
        self.connection_generation = 0
        self.phase = self.DISCONNECTED
        self._snapshot_sequence: int | None = None
        self._last_sequence: int | None = None
        self._snapshot_evidence_ref: str | None = None
        self._buffered_updates: list[KrakenSpotExecutionFrame] = []
        self._snapshot_order_ids: tuple[str, ...] = ()
        self._gap_expected_sequence: int | None = None
        self._gap_observed_sequence: int | None = None
        self._gap_evidence_ref: str | None = None

    def begin_connection(self) -> int:
        """Start/restart a stream generation and require a fresh snapshot."""

        self.connection_generation += 1
        self.phase = self.AWAITING_SNAPSHOT
        self._snapshot_sequence = None
        self._last_sequence = None
        self._snapshot_evidence_ref = None
        self._buffered_updates.clear()
        self._snapshot_order_ids = ()
        self._gap_expected_sequence = None
        self._gap_observed_sequence = None
        self._gap_evidence_ref = None
        return self.connection_generation

    def disconnect(self) -> None:
        """Invalidate all stream-local state; stale orders are never reusable."""

        self.phase = self.DISCONNECTED
        self._snapshot_sequence = None
        self._last_sequence = None
        self._snapshot_evidence_ref = None
        self._buffered_updates.clear()
        self._snapshot_order_ids = ()
        self._gap_expected_sequence = None
        self._gap_observed_sequence = None
        self._gap_evidence_ref = None

    def _require_scope(self, frame: KrakenSpotExecutionFrame) -> None:
        if not isinstance(frame, KrakenSpotExecutionFrame):
            raise TypeError("frame must be KrakenSpotExecutionFrame")
        if (
            frame.account_id != self.account_id
            or frame.environment != self.environment
        ):
            raise KrakenSpotStreamError(
                "Kraken stream frame account/environment scope mismatch"
            )

    def _enter_gap(
        self,
        *,
        expected: int,
        observed: int,
        evidence_ref: str,
    ) -> None:
        self.phase = self.GAP_RECONCILIATION_REQUIRED
        self._gap_expected_sequence = expected
        self._gap_observed_sequence = observed
        self._gap_evidence_ref = evidence_ref
        self._snapshot_order_ids = ()
        self._buffered_updates.clear()

    def apply_frame(self, frame: KrakenSpotExecutionFrame) -> None:
        """Consume one frame while keeping reconciliation as the READY authority."""

        self._require_scope(frame)
        if self.phase == self.DISCONNECTED:
            raise KrakenSpotStreamError(
                "Kraken stream frame received while disconnected"
            )
        if self.phase == self.GAP_RECONCILIATION_REQUIRED:
            raise KrakenSpotStreamError(
                "Kraken stream gap requires a fresh connection and reconciliation"
            )

        if self.phase == self.AWAITING_SNAPSHOT:
            if frame.frame_type != "snapshot":
                raise KrakenSpotStreamError(
                    "Kraken stream generation requires snapshot before updates"
                )
            terminal = sorted(
                {
                    report.order_id
                    for report in frame.reports
                    if report.order_status in _TERMINAL_ORDER_STATUSES
                }
            )
            if terminal:
                raise KrakenSpotStreamError(
                    "Kraken open-order snapshot contains terminal orders"
                )
            self._snapshot_sequence = frame.sequence
            self._last_sequence = frame.sequence
            self._snapshot_evidence_ref = frame.evidence_ref
            self._snapshot_order_ids = tuple(
                sorted({report.order_id for report in frame.reports})
            )
            self.phase = self.REST_RECONCILIATION_REQUIRED
            return

        if frame.frame_type != "update":
            raise KrakenSpotStreamError(
                "Kraken stream accepts only updates after the snapshot"
            )
        if self._last_sequence is None:
            raise KrakenSpotStreamError(
                "Kraken stream sequence state is unavailable"
            )
        expected = self._last_sequence + 1
        if frame.sequence != expected:
            self._enter_gap(
                expected=expected,
                observed=frame.sequence,
                evidence_ref=frame.evidence_ref,
            )
            return
        if len(self._buffered_updates) >= self.max_buffered_updates:
            self._enter_gap(
                expected=expected,
                observed=frame.sequence,
                evidence_ref=frame.evidence_ref,
            )
            return
        self._buffered_updates.append(frame)
        self._last_sequence = frame.sequence

    def evidence(self) -> KrakenSpotStreamRecoveryEvidence:
        """Return an immutable, explicitly non-READY reconciliation handoff."""

        return KrakenSpotStreamRecoveryEvidence(
            account_id=self.account_id,
            environment=self.environment,
            connection_generation=self.connection_generation,
            phase=self.phase,
            snapshot_sequence=self._snapshot_sequence,
            last_sequence=self._last_sequence,
            snapshot_evidence_ref=self._snapshot_evidence_ref,
            buffered_update_evidence_refs=tuple(
                frame.evidence_ref for frame in self._buffered_updates
            ),
            provisional_snapshot_order_ids=self._snapshot_order_ids,
            gap_expected_sequence=self._gap_expected_sequence,
            gap_observed_sequence=self._gap_observed_sequence,
            gap_evidence_ref=self._gap_evidence_ref,
        )
