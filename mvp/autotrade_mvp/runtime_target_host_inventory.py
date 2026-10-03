"""Canonical raw target-host inventory collector for WP-65.

This module captures only stable runtime/host facts already used by the WP-65
load campaign. It does not create qualification, chronology, release, provider,
or trading authority. In particular, it records no local wall-clock timestamp:
terminal chronology is a separate independently authenticated boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping

from .runtime_load_campaign import capture_runtime_host_identity


SCHEMA_VERSION = "1.0.0"
EVIDENCE_TYPE = "AUTOTRADE_RUNTIME_TARGET_HOST_INVENTORY"
COLLECTOR_ID = "autotrade-runtime-target-host-inventory"
COLLECTOR_VERSION = "1.0.0"

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HOST_IDENTITY_KEYS = frozenset(
    {
        "system",
        "release",
        "machine",
        "python_implementation",
        "python_version",
        "cpu_count",
    }
)


class RuntimeTargetHostInventoryError(ValueError):
    """Raised when target-host inventory cannot be canonically established."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeTargetHostInventoryError(
            f"{name} must be canonical non-empty text"
        )
    if "\n" in value or "\r" in value:
        raise RuntimeTargetHostInventoryError(
            f"{name} must not contain line breaks"
        )
    return value


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256_RE.fullmatch(text) is None:
        raise RuntimeTargetHostInventoryError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return text


def _normalize_host_identity(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeTargetHostInventoryError("host identity must be a mapping")
    identity = dict(value)
    if set(identity) != _HOST_IDENTITY_KEYS:
        raise RuntimeTargetHostInventoryError(
            "host identity fields do not match the canonical runtime identity"
        )
    normalized: dict[str, object] = {}
    for field in (
        "system",
        "release",
        "machine",
        "python_implementation",
        "python_version",
    ):
        normalized[field] = _text(identity[field], name=f"host_identity.{field}")
    cpu_count = identity["cpu_count"]
    if type(cpu_count) is not int or cpu_count <= 0:
        raise RuntimeTargetHostInventoryError(
            "host_identity.cpu_count must be a positive integer"
        )
    normalized["cpu_count"] = cpu_count
    return normalized


def host_identity_fingerprint(identity: object) -> str:
    """Return the canonical fingerprint used by the existing load campaign."""

    normalized = _normalize_host_identity(identity)
    return "sha256:" + sha256(_canonical_json(normalized)).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostInventory:
    host_identity: Mapping[str, object]
    host_fingerprint: str
    collector_id: str = COLLECTOR_ID
    collector_version: str = COLLECTOR_VERSION
    schema_version: str = SCHEMA_VERSION
    evidence_type: str = EVIDENCE_TYPE

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RuntimeTargetHostInventoryError(
                "unsupported target-host inventory schema_version"
            )
        if self.evidence_type != EVIDENCE_TYPE:
            raise RuntimeTargetHostInventoryError(
                "unsupported target-host inventory evidence_type"
            )
        if self.collector_id != COLLECTOR_ID:
            raise RuntimeTargetHostInventoryError(
                "target-host inventory collector_id is not canonical"
            )
        if self.collector_version != COLLECTOR_VERSION:
            raise RuntimeTargetHostInventoryError(
                "target-host inventory collector_version is not canonical"
            )
        normalized = _normalize_host_identity(self.host_identity)
        fingerprint = _digest(
            self.host_fingerprint,
            name="host_fingerprint",
        )
        observed = host_identity_fingerprint(normalized)
        if fingerprint != observed:
            raise RuntimeTargetHostInventoryError(
                "target-host inventory fingerprint does not match captured identity"
            )
        object.__setattr__(self, "host_identity", MappingProxyType(normalized))
        object.__setattr__(self, "host_fingerprint", fingerprint)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "collector_id": self.collector_id,
            "collector_version": self.collector_version,
            "evidence_type": self.evidence_type,
            "host_fingerprint": self.host_fingerprint,
            "host_identity": dict(self.host_identity),
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_payload())

    @property
    def payload_sha256(self) -> str:
        return "sha256:" + sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def parse(cls, raw: bytes) -> "RuntimeTargetHostInventory":
        if type(raw) is not bytes or not raw:
            raise RuntimeTargetHostInventoryError(
                "target-host inventory must be non-empty bytes"
            )

        def reject_duplicates(
            pairs: list[tuple[str, object]],
        ) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, item in pairs:
                if key in result:
                    raise RuntimeTargetHostInventoryError(
                        "target-host inventory contains duplicate JSON key"
                    )
                result[key] = item
            return result

        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    RuntimeTargetHostInventoryError(
                        f"target-host inventory contains invalid JSON constant {token}"
                    )
                ),
            )
        except (UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeTargetHostInventoryError(
                "target-host inventory is not valid UTF-8 JSON"
            ) from error
        if type(value) is not dict or set(value) != {
            "collector_id",
            "collector_version",
            "evidence_type",
            "host_fingerprint",
            "host_identity",
            "schema_version",
        }:
            raise RuntimeTargetHostInventoryError(
                "target-host inventory fields are non-canonical"
            )
        inventory = cls(
            host_identity=value["host_identity"],
            host_fingerprint=value["host_fingerprint"],
            collector_id=value["collector_id"],
            collector_version=value["collector_version"],
            schema_version=value["schema_version"],
            evidence_type=value["evidence_type"],
        )
        if inventory.canonical_bytes() != raw:
            raise RuntimeTargetHostInventoryError(
                "target-host inventory bytes are not canonical JSON"
            )
        return inventory


def collect_runtime_target_host_inventory(
    *,
    expected_host_fingerprint: str,
) -> RuntimeTargetHostInventory:
    """Capture the real host identity and require its predeclared fingerprint."""

    expected = _digest(
        expected_host_fingerprint,
        name="expected_host_fingerprint",
    )
    identity = _normalize_host_identity(capture_runtime_host_identity())
    observed = host_identity_fingerprint(identity)
    if observed != expected:
        raise RuntimeTargetHostInventoryError(
            "captured target host does not match predeclared host fingerprint"
        )
    return RuntimeTargetHostInventory(
        host_identity=identity,
        host_fingerprint=observed,
    )
