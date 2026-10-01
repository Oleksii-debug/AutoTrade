"""Source-owned private-stream frame parsers selected by accepted Q semantics.

Parsing exact bytes is deliberately separate from socket/frame origin. A parsed
frame is not PAPER/LIVE provider evidence until a product-issued authenticated
connection/frame receipt binds the same bytes and accepted provider authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Callable, Mapping

from .provider_qualification_authority import PrivateStreamSemantics
from .provider_response_limits import require_provider_json_depth


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UINT64_MAX = (1 << 64) - 1
_INT64_MAX = (1 << 63) - 1
_BYBIT_EXECUTION_CATEGORIES = frozenset({"spot", "linear", "inverse", "option"})


class PrivateStreamParserError(ValueError):
    """Exact-frame parsing or installed-parser selection failed."""


@dataclass(frozen=True, slots=True)
class InstalledPrivateStreamParser:
    topic_id: str
    parser_id: str
    parser_version: str
    sequence_policy: str
    sequence_scope: str
    recovery_method_id: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.parser_id, self.parser_version)


@dataclass(frozen=True, slots=True)
class ParsedPrivateStreamEvent:
    provider_event_id: str
    provider_sequence: str | None
    sequence_scope_value: str | None
    category: str
    symbol: str
    provider_event_time_ms: str
    data_index: int

    def __post_init__(self) -> None:
        for name in ("provider_event_id", "category", "symbol"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise PrivateStreamParserError(f"{name} must be canonical non-empty text")
        if (
            type(self.data_index) is not int
            or self.data_index < 0
        ):
            raise PrivateStreamParserError("data_index must be a non-negative integer")
        if self.provider_sequence is not None and (
            type(self.provider_sequence) is not str
            or re.fullmatch(r"(?:0|[1-9][0-9]*)", self.provider_sequence) is None
        ):
            raise PrivateStreamParserError(
                "provider_sequence must be canonical non-negative integer text"
            )
        if self.sequence_scope_value is not None and (
            type(self.sequence_scope_value) is not str
            or not self.sequence_scope_value
            or self.sequence_scope_value != self.sequence_scope_value.strip()
        ):
            raise PrivateStreamParserError(
                "sequence_scope_value must be canonical non-empty text"
            )
        _canonical_uint_text(
            self.provider_event_time_ms,
            name="provider_event_time_ms",
            max_value=_UINT64_MAX,
        )


@dataclass(frozen=True, slots=True)
class ParsedPrivateStreamFrame:
    parser: InstalledPrivateStreamParser
    frame_sha256: str
    provider_message_id: str
    provider_creation_time_ms: int
    events: tuple[ParsedPrivateStreamEvent, ...]

    def __post_init__(self) -> None:
        if type(self.parser) is not InstalledPrivateStreamParser:
            raise TypeError("parser must be exact InstalledPrivateStreamParser")
        if type(self.frame_sha256) is not str or _SHA256_RE.fullmatch(
            self.frame_sha256
        ) is None:
            raise PrivateStreamParserError("frame_sha256 must be canonical SHA-256")
        if (
            type(self.provider_message_id) is not str
            or not self.provider_message_id
            or self.provider_message_id != self.provider_message_id.strip()
        ):
            raise PrivateStreamParserError(
                "provider_message_id must be canonical non-empty text"
            )
        _exact_uint(
            self.provider_creation_time_ms,
            name="provider_creation_time_ms",
            max_value=_UINT64_MAX,
        )
        if (
            type(self.events) is not tuple
            or not self.events
            or not all(type(item) is ParsedPrivateStreamEvent for item in self.events)
        ):
            raise PrivateStreamParserError(
                "parsed private-stream frame must contain exact parsed events"
            )


_BYBIT_V5_PRIVATE_EXECUTION = InstalledPrivateStreamParser(
    topic_id="execution",
    parser_id="bybit-v5-private-execution",
    parser_version="1.0.0",
    sequence_policy="MONOTONIC_NONCONTIGUOUS",
    sequence_scope="symbol",
    recovery_method_id="snapshot-readback-v1",
)


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PrivateStreamParserError(
                "private-stream frame contains duplicate JSON keys"
            )
        result[key] = value
    return result


def _reject_nonfinite(value: str):
    raise PrivateStreamParserError(
        "private-stream frame contains non-finite JSON number"
    )


def _canonical_member(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PrivateStreamParserError(f"{name} must be canonical non-empty text")
    return value


def _exact_uint(value: object, *, name: str, max_value: int) -> int:
    if (
        type(value) is not int
        or value < 0
        or value > max_value
    ):
        raise PrivateStreamParserError(
            f"{name} must be an exact non-negative integer within the installed bound"
        )
    return value


def _canonical_uint_text(value: object, *, name: str, max_value: int) -> str:
    if (
        type(value) is not str
        or re.fullmatch(r"(?:0|[1-9][0-9]*)", value) is None
    ):
        raise PrivateStreamParserError(
            f"{name} must be canonical non-negative integer text"
        )
    if int(value) > max_value:
        raise PrivateStreamParserError(
            f"{name} exceeds the installed integer bound"
        )
    return value


def _parse_bybit_v5_private_execution(
    frame_bytes: bytes,
) -> ParsedPrivateStreamFrame:
    if type(frame_bytes) is not bytes or not frame_bytes:
        raise PrivateStreamParserError(
            "private-stream frame must be non-empty exact bytes"
        )
    try:
        require_provider_json_depth(frame_bytes)
    except ValueError as error:
        raise PrivateStreamParserError(
            "private-stream frame exceeds shared JSON resource budget"
        ) from error
    try:
        text = frame_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise PrivateStreamParserError(
            "private-stream frame must be strict UTF-8 JSON"
        ) from error
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except PrivateStreamParserError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise PrivateStreamParserError(
            "private-stream frame must be one valid JSON object"
        ) from error
    if type(payload) is not dict:
        raise PrivateStreamParserError(
            "private-stream frame root must be an exact JSON object"
        )
    if payload.get("topic") != "execution":
        raise PrivateStreamParserError(
            "Bybit V5 private execution parser requires exact execution topic"
        )
    message_id = _canonical_member(
        payload.get("id"),
        name="provider message id",
    )
    creation_time = _exact_uint(
        payload.get("creationTime"),
        name="Bybit V5 execution creationTime",
        max_value=_UINT64_MAX,
    )
    data = payload.get("data")
    if type(data) is not list or not data:
        raise PrivateStreamParserError(
            "Bybit V5 private execution frame requires non-empty data array"
        )

    events: list[ParsedPrivateStreamEvent] = []
    seen_exec_ids: set[str] = set()
    for index, item in enumerate(data):
        if type(item) is not dict:
            raise PrivateStreamParserError(
                "Bybit V5 execution data item must be an exact JSON object"
            )
        event_id = _canonical_member(item.get("execId"), name="execId")
        if event_id in seen_exec_ids:
            raise PrivateStreamParserError(
                "Bybit V5 execution frame repeats execId"
            )
        seen_exec_ids.add(event_id)
        symbol = _canonical_member(item.get("symbol"), name="symbol")
        category = _canonical_member(item.get("category"), name="category")
        if category not in _BYBIT_EXECUTION_CATEGORIES:
            raise PrivateStreamParserError(
                "Bybit V5 execution category is outside the installed product set"
            )
        event_time = _canonical_uint_text(
            item.get("execTime"),
            name="Bybit V5 execution execTime",
            max_value=_UINT64_MAX,
        )
        sequence = _exact_uint(
            item.get("seq"),
            name="Bybit V5 execution seq",
            max_value=_INT64_MAX,
        )
        events.append(
            ParsedPrivateStreamEvent(
                provider_event_id=event_id,
                provider_sequence=str(sequence),
                sequence_scope_value=symbol,
                category=category,
                symbol=symbol,
                provider_event_time_ms=event_time,
                data_index=index,
            )
        )
    return ParsedPrivateStreamFrame(
        parser=_BYBIT_V5_PRIVATE_EXECUTION,
        frame_sha256="sha256:" + sha256(frame_bytes).hexdigest(),
        provider_message_id=message_id,
        provider_creation_time_ms=creation_time,
        events=tuple(events),
    )


_PARSER_IMPLS: Mapping[
    tuple[str, str], Callable[[bytes], ParsedPrivateStreamFrame]
] = MappingProxyType(
    {
        _BYBIT_V5_PRIVATE_EXECUTION.key: _parse_bybit_v5_private_execution,
    }
)
_PARSER_DESCRIPTORS: Mapping[
    tuple[str, str], InstalledPrivateStreamParser
] = MappingProxyType(
    {
        _BYBIT_V5_PRIVATE_EXECUTION.key: _BYBIT_V5_PRIVATE_EXECUTION,
    }
)


def installed_private_stream_parser(
    semantics: PrivateStreamSemantics,
) -> InstalledPrivateStreamParser:
    """Resolve one source-installed parser from one exact accepted-Q stream contract."""

    if type(semantics) is not PrivateStreamSemantics:
        raise PrivateStreamParserError(
            "private-stream parser selection requires exact accepted-Q semantics"
        )
    descriptor = _PARSER_DESCRIPTORS.get(
        (semantics.parser_id, semantics.parser_version)
    )
    if descriptor is None:
        raise PrivateStreamParserError(
            "private-stream parser id/version is not source-installed"
        )
    supplied = (
        semantics.topic_id,
        semantics.parser_id,
        semantics.parser_version,
        semantics.sequence_policy,
        semantics.sequence_scope,
        semantics.recovery_method_id,
    )
    expected = (
        descriptor.topic_id,
        descriptor.parser_id,
        descriptor.parser_version,
        descriptor.sequence_policy,
        descriptor.sequence_scope,
        descriptor.recovery_method_id,
    )
    if supplied != expected:
        raise PrivateStreamParserError(
            "private-stream parser selection does not match installed Q semantics"
        )
    return descriptor


def parse_installed_private_stream_frame(
    *,
    semantics: PrivateStreamSemantics,
    frame_bytes: object,
) -> ParsedPrivateStreamFrame:
    """Parse held exact frame bytes under one exact accepted-Q parser contract."""

    descriptor = installed_private_stream_parser(semantics)
    if type(frame_bytes) is not bytes:
        raise PrivateStreamParserError(
            "private-stream frame must be exact bytes"
        )
    parser = _PARSER_IMPLS[descriptor.key]
    parsed = parser(frame_bytes)
    if type(parsed) is not ParsedPrivateStreamFrame or parsed.parser != descriptor:
        raise PrivateStreamParserError(
            "source-installed private-stream parser returned invalid authority shape"
        )
    return parsed
