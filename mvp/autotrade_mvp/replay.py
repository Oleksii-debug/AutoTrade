from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence
from weakref import WeakKeyDictionary


class ReplayError(ValueError):
    pass


def _instant(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReplayError(f"{field} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ReplayError(f"{field} must be an ISO-8601 timestamp") from error
    return parsed.astimezone(timezone.utc)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _canonical_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("payload must be a mapping")
    normalized = json.loads(
        json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )
    if not isinstance(normalized, dict):
        raise TypeError("payload must serialize as an object")
    frozen = _deep_freeze(normalized)
    if not isinstance(frozen, Mapping):
        raise TypeError("payload must freeze as an object")
    return frozen


@dataclass(frozen=True)
class ReplayEvent:
    sequence: int
    available_at: str
    source_version: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise TypeError("sequence must be an integer")
        if self.sequence < 0:
            raise ReplayError("sequence must be non-negative")
        _instant(self.available_at, field="available_at")
        if not isinstance(self.source_version, str) or not self.source_version.strip():
            raise ReplayError("source_version must be non-empty")
        object.__setattr__(self, "payload", _canonical_payload(self.payload))


_SHA256_HEX = frozenset("0123456789abcdef")
REQUIRED_RUNTIME_COMPONENTS = frozenset(
    {
        "pending_event_queue",
        "rng_state",
        "strategy_state",
        "portfolio_accounting_state",
        "execution_state",
        "accrual_state",
        "policy_state",
        "instrument_state",
        "provider_state",
        "experiment_state",
    }
)


def _sha256_hex(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _SHA256_HEX for character in value)
    ):
        raise ReplayError(f"{field} must be a canonical lowercase SHA-256 hex digest")
    return value


def _signature_hex(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) < 64
        or len(value) > 8192
        or len(value) % 2
        or any(character not in _SHA256_HEX for character in value)
    ):
        raise ReplayError(
            f"{field} must be bounded canonical lowercase signature hex"
        )
    return value


def _build_sha(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) not in {40, 64}
        or any(character not in _SHA256_HEX for character in value)
    ):
        raise ReplayError("build_sha must be a canonical lowercase Git object hash")
    return value


def _component_bindings(
    values: Mapping[str, str],
) -> Mapping[str, str]:
    if not isinstance(values, Mapping):
        raise TypeError("runtime component bindings must be a mapping")
    normalized: dict[str, str] = {}
    for raw_name, raw_digest in values.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ReplayError("runtime component names must be non-empty")
        name = raw_name.strip()
        if name != raw_name:
            raise ReplayError("runtime component names must be canonical text")
        if name in normalized:
            raise ReplayError("runtime component names must be unique")
        normalized[name] = _sha256_hex(
            raw_digest,
            field=f"runtime component {name}",
        )
    missing = sorted(REQUIRED_RUNTIME_COMPONENTS - set(normalized))
    if missing:
        raise ReplayError(
            "composite replay checkpoint is missing required runtime components: "
            + ", ".join(missing)
        )
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True)
class RuntimeStateSnapshot:
    """One composition-authority-issued whole-runtime cut.

    authority_seal is an HMAC over the exact replay cut, common-cut identity
    and component digests. The secret is owned by the composition authority and
    is deliberately not persisted in replay checkpoints.
    """

    cut_id: str
    replay: "ReplayCheckpoint"
    runtime_components: Mapping[str, str]
    authority_id: str
    verifier_id: str
    authority_seal: str

    def __post_init__(self) -> None:
        if not isinstance(self.cut_id, str) or not self.cut_id.strip():
            raise ReplayError("runtime cut_id must be non-empty")
        if self.cut_id != self.cut_id.strip():
            raise ReplayError("runtime cut_id must be canonical text")
        if not isinstance(self.replay, ReplayCheckpoint):
            raise TypeError("runtime snapshot replay must be ReplayCheckpoint")
        if not isinstance(self.authority_id, str) or not self.authority_id.strip():
            raise ReplayError("runtime authority_id must be non-empty")
        if self.authority_id != self.authority_id.strip():
            raise ReplayError("runtime authority_id must be canonical text")
        if not isinstance(self.verifier_id, str) or not self.verifier_id.strip():
            raise ReplayError("runtime verifier_id must be non-empty")
        if self.verifier_id != self.verifier_id.strip():
            raise ReplayError("runtime verifier_id must be canonical text")
        _signature_hex(self.authority_seal, field="runtime authority signature")
        object.__setattr__(
            self,
            "runtime_components",
            _component_bindings(self.runtime_components),
        )


RuntimeStateCutResolver = Callable[
    [], tuple[str, "ReplayCheckpoint", Mapping[str, str]]
]
RuntimeStateSigner = Callable[[bytes], str]
RuntimeStateSignatureVerifier = Callable[[bytes, str], bool]


def _runtime_authority_state_operations():
    authority_states = WeakKeyDictionary()
    verifier_states = WeakKeyDictionary()

    def register_authority(
        authority: "RuntimeStateAuthority",
        authority_id: str,
        signer: RuntimeStateSigner,
        cut_resolver: RuntimeStateCutResolver,
    ) -> None:
        authority_states[authority] = (authority_id, signer, cut_resolver)

    def authority_id(authority: "RuntimeStateAuthority") -> str:
        try:
            return authority_states[authority][0]
        except KeyError as error:
            raise ReplayError(
                "runtime state authority process state is unavailable"
            ) from error

    def resolve_cut(
        authority: "RuntimeStateAuthority",
    ) -> tuple[str, "ReplayCheckpoint", Mapping[str, str]]:
        try:
            resolver = authority_states[authority][2]
        except KeyError as error:
            raise ReplayError(
                "runtime state authority process state is unavailable"
            ) from error
        return resolver()

    def sign(authority: "RuntimeStateAuthority", material: bytes) -> str:
        try:
            signer = authority_states[authority][1]
        except KeyError as error:
            raise ReplayError(
                "runtime state authority process state is unavailable"
            ) from error
        try:
            signature = signer(material)
        except Exception as error:
            raise ReplayError("runtime state signer failed") from error
        return _signature_hex(
            signature,
            field="runtime authority signature",
        )

    def register_verifier(
        verifier: "RuntimeStateVerifier",
        authority_id_value: str,
        verifier_id_value: str,
        verify_signature: RuntimeStateSignatureVerifier,
    ) -> None:
        verifier_states[verifier] = (
            authority_id_value,
            verifier_id_value,
            verify_signature,
        )

    def verifier_authority_id(verifier: "RuntimeStateVerifier") -> str:
        try:
            return verifier_states[verifier][0]
        except KeyError as error:
            raise ReplayError(
                "runtime state verifier process state is unavailable"
            ) from error

    def verifier_id(verifier: "RuntimeStateVerifier") -> str:
        try:
            return verifier_states[verifier][1]
        except KeyError as error:
            raise ReplayError(
                "runtime state verifier process state is unavailable"
            ) from error

    def verify_signature(
        verifier: "RuntimeStateVerifier",
        material: bytes,
        signature: str,
    ) -> None:
        try:
            verify = verifier_states[verifier][2]
        except KeyError as error:
            raise ReplayError(
                "runtime state verifier process state is unavailable"
            ) from error
        signature = _signature_hex(
            signature,
            field="runtime authority signature",
        )
        try:
            accepted = verify(material, signature)
        except Exception as error:
            raise ReplayError("runtime state verifier failed closed") from error
        if accepted is not True:
            raise ReplayError("runtime state authority signature mismatch")

    return (
        register_authority,
        authority_id,
        resolve_cut,
        sign,
        register_verifier,
        verifier_authority_id,
        verifier_id,
        verify_signature,
    )


(
    _register_runtime_state_authority,
    _runtime_state_authority_id,
    _resolve_runtime_authority_cut,
    _sign_runtime_authority_material,
    _register_runtime_state_verifier,
    _runtime_state_verifier_authority_id,
    _runtime_state_verifier_id,
    _verify_runtime_state_signature,
) = _runtime_authority_state_operations()
del _runtime_authority_state_operations


class RuntimeStateVerifier:
    """Separately provisioned trust anchor for one runtime-state authority."""

    __slots__ = ("__weakref__",)

    def __init__(
        self,
        *,
        authority_id: str,
        verifier_id: str,
        verify_signature: RuntimeStateSignatureVerifier,
    ) -> None:
        for value, field in (
            (authority_id, "runtime authority_id"),
            (verifier_id, "runtime verifier_id"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ReplayError(f"{field} must be non-empty")
            if value != value.strip():
                raise ReplayError(f"{field} must be canonical text")
        if not callable(verify_signature):
            raise TypeError("runtime verify_signature must be callable")
        _register_runtime_state_verifier(
            self,
            authority_id,
            verifier_id,
            verify_signature,
        )

    @property
    def authority_id(self) -> str:
        return _runtime_state_verifier_authority_id(self)

    @property
    def verifier_id(self) -> str:
        return _runtime_state_verifier_id(self)

    def verify_snapshot(self, snapshot: RuntimeStateSnapshot) -> None:
        if type(snapshot) is not RuntimeStateSnapshot:
            raise ReplayError(
                "runtime state verifier requires canonical RuntimeStateSnapshot"
            )
        if snapshot.authority_id != self.authority_id:
            raise ReplayError("runtime state snapshot authority identity mismatch")
        if snapshot.verifier_id != self.verifier_id:
            raise ReplayError("runtime state snapshot verifier identity mismatch")
        _verify_runtime_state_signature(
            self,
            RuntimeStateAuthority._binding_material(
                authority_id=snapshot.authority_id,
                verifier_id=snapshot.verifier_id,
                cut_id=snapshot.cut_id,
                replay=snapshot.replay,
                runtime_components=snapshot.runtime_components,
            ),
            snapshot.authority_seal,
        )

    def verify_checkpoint_binding(
        self,
        checkpoint: "CompositeReplayCheckpoint",
    ) -> None:
        if type(checkpoint) is not CompositeReplayCheckpoint:
            raise ReplayError(
                "runtime state verifier requires canonical CompositeReplayCheckpoint"
            )
        if checkpoint.runtime_authority_id != self.authority_id:
            raise ReplayError("runtime state checkpoint authority identity mismatch")
        if checkpoint.runtime_verifier_id != self.verifier_id:
            raise ReplayError("runtime state checkpoint verifier identity mismatch")
        _verify_runtime_state_signature(
            self,
            RuntimeStateAuthority._checkpoint_binding_material(
                authority_id=checkpoint.runtime_authority_id,
                verifier_id=checkpoint.runtime_verifier_id,
                cut_id=checkpoint.runtime_cut_id,
                replay=checkpoint.replay,
                runtime_components=checkpoint.runtime_components,
                build_sha=checkpoint.build_sha,
                protocol_ref=checkpoint.protocol_ref,
            ),
            checkpoint.runtime_authority_seal,
        )


class RuntimeStateAuthority:
    """Signing side for common runtime cuts; trust is supplied separately."""

    __slots__ = ("__weakref__",)

    def __init__(
        self,
        *,
        authority_id: str,
        signer: RuntimeStateSigner,
        cut_resolver: RuntimeStateCutResolver,
    ) -> None:
        if not isinstance(authority_id, str) or not authority_id.strip():
            raise ReplayError("runtime authority_id must be non-empty")
        if authority_id != authority_id.strip():
            raise ReplayError("runtime authority_id must be canonical text")
        if not callable(signer):
            raise TypeError("runtime signer must be callable")
        if not callable(cut_resolver):
            raise TypeError("runtime cut_resolver must be callable")
        _register_runtime_state_authority(
            self,
            authority_id,
            signer,
            cut_resolver,
        )

    @property
    def authority_id(self) -> str:
        return _runtime_state_authority_id(self)

    @staticmethod
    def _binding_material(
        *,
        authority_id: str,
        verifier_id: str,
        cut_id: str,
        replay: "ReplayCheckpoint",
        runtime_components: Mapping[str, str],
    ) -> bytes:
        material = {
            "binding_kind": "runtime_state_snapshot-v1",
            "authority_id": authority_id,
            "verifier_id": verifier_id,
            "cut_id": cut_id,
            "replay": {
                "dataset_digest": replay.dataset_digest,
                "cursor": replay.cursor,
                "clock": replay.clock,
            },
            "runtime_components": dict(runtime_components),
        }
        return json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")

    @staticmethod
    def _checkpoint_binding_material(
        *,
        authority_id: str,
        verifier_id: str,
        cut_id: str,
        replay: "ReplayCheckpoint",
        runtime_components: Mapping[str, str],
        build_sha: str,
        protocol_ref: str,
    ) -> bytes:
        build = _build_sha(build_sha)
        if not isinstance(protocol_ref, str) or not protocol_ref.strip():
            raise ReplayError("protocol_ref must be non-empty")
        protocol = protocol_ref.strip()
        if protocol != protocol_ref:
            raise ReplayError("protocol_ref must be canonical text")
        material = {
            "binding_kind": "composite_replay_checkpoint-v1",
            "authority_id": authority_id,
            "verifier_id": verifier_id,
            "cut_id": cut_id,
            "replay": {
                "dataset_digest": replay.dataset_digest,
                "cursor": replay.cursor,
                "clock": replay.clock,
            },
            "runtime_components": dict(runtime_components),
            "build_sha": build,
            "protocol_ref": protocol,
        }
        return json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")

    def capture(
        self,
        *,
        verifier_id: str,
    ) -> RuntimeStateSnapshot:
        authority_id = _runtime_state_authority_id(self)
        if not isinstance(verifier_id, str) or not verifier_id.strip():
            raise ReplayError("runtime verifier_id must be non-empty")
        if verifier_id != verifier_id.strip():
            raise ReplayError("runtime verifier_id must be canonical text")
        try:
            cut_id, replay, raw_components = _resolve_runtime_authority_cut(self)
        except Exception as error:
            raise ReplayError("runtime state authority failed") from error
        if not isinstance(cut_id, str) or not cut_id.strip():
            raise ReplayError("runtime cut_id must be non-empty")
        if cut_id != cut_id.strip():
            raise ReplayError("runtime cut_id must be canonical text")
        if not isinstance(replay, ReplayCheckpoint):
            raise ReplayError(
                "runtime state authority must resolve a ReplayCheckpoint"
            )
        components = _component_bindings(raw_components)
        material = self._binding_material(
            authority_id=authority_id,
            verifier_id=verifier_id,
            cut_id=cut_id,
            replay=replay,
            runtime_components=components,
        )
        return RuntimeStateSnapshot(
            cut_id=cut_id,
            replay=replay,
            runtime_components=components,
            authority_id=authority_id,
            verifier_id=verifier_id,
            authority_seal=_sign_runtime_authority_material(self, material),
        )

    def seal_checkpoint(
        self,
        snapshot: RuntimeStateSnapshot,
        *,
        build_sha: str,
        protocol_ref: str,
    ) -> str:
        if type(snapshot) is not RuntimeStateSnapshot:
            raise ReplayError(
                "runtime state authority requires canonical RuntimeStateSnapshot"
            )
        if snapshot.authority_id != self.authority_id:
            raise ReplayError("runtime state snapshot authority identity mismatch")
        return _sign_runtime_authority_material(
            self,
            self._checkpoint_binding_material(
                authority_id=snapshot.authority_id,
                verifier_id=snapshot.verifier_id,
                cut_id=snapshot.cut_id,
                replay=snapshot.replay,
                runtime_components=snapshot.runtime_components,
                build_sha=build_sha,
                protocol_ref=protocol_ref,
            ),
        )

def _resolve_runtime_snapshot(
    authority: RuntimeStateAuthority,
    verifier: RuntimeStateVerifier,
) -> RuntimeStateSnapshot:
    if type(authority) is not RuntimeStateAuthority:
        raise TypeError(
            "runtime_state_authority must be the canonical RuntimeStateAuthority"
        )
    if type(verifier) is not RuntimeStateVerifier:
        raise TypeError(
            "runtime_state_verifier must be the canonical RuntimeStateVerifier"
        )
    if authority.authority_id != verifier.authority_id:
        raise ReplayError("runtime authority and verifier identities differ")
    snapshot = RuntimeStateAuthority.capture(
        authority,
        verifier_id=verifier.verifier_id,
    )
    RuntimeStateVerifier.verify_snapshot(verifier, snapshot)
    return snapshot


@dataclass(frozen=True)
class CompositeReplayCheckpoint:
    """Immutable whole-runtime resume gate over existing component authorities.

    Component digests are references to canonical state owned elsewhere; this
    envelope does not become a second ledger, scheduler, strategy store or RNG
    authority. Resume is permitted only when every bound component, build and
    experiment/protocol identity matches before another replay event is exposed.
    """

    replay: "ReplayCheckpoint"
    runtime_components: Mapping[str, str]
    runtime_cut_id: str
    runtime_authority_id: str
    runtime_verifier_id: str
    runtime_authority_seal: str
    build_sha: str
    protocol_ref: str
    schema_version: str = "4.0.0"

    def __post_init__(self) -> None:
        if not isinstance(self.replay, ReplayCheckpoint):
            raise TypeError("replay must be ReplayCheckpoint")
        components = _component_bindings(self.runtime_components)
        if not isinstance(self.runtime_cut_id, str) or not self.runtime_cut_id.strip():
            raise ReplayError("runtime_cut_id must be non-empty")
        cut_id = self.runtime_cut_id.strip()
        if cut_id != self.runtime_cut_id:
            raise ReplayError("runtime_cut_id must be canonical text")
        if not isinstance(self.runtime_authority_id, str) or not self.runtime_authority_id.strip():
            raise ReplayError("runtime_authority_id must be non-empty")
        authority_id = self.runtime_authority_id.strip()
        if authority_id != self.runtime_authority_id:
            raise ReplayError("runtime_authority_id must be canonical text")
        if not isinstance(self.runtime_verifier_id, str) or not self.runtime_verifier_id.strip():
            raise ReplayError("runtime_verifier_id must be non-empty")
        verifier_id = self.runtime_verifier_id.strip()
        if verifier_id != self.runtime_verifier_id:
            raise ReplayError("runtime_verifier_id must be canonical text")
        authority_seal = _signature_hex(
            self.runtime_authority_seal,
            field="runtime authority signature",
        )
        build = _build_sha(self.build_sha)
        if not isinstance(self.protocol_ref, str) or not self.protocol_ref.strip():
            raise ReplayError("protocol_ref must be non-empty")
        protocol = self.protocol_ref.strip()
        if protocol != self.protocol_ref:
            raise ReplayError("protocol_ref must be canonical text")
        if self.schema_version != "4.0.0":
            raise ReplayError("unsupported composite replay checkpoint schema")
        object.__setattr__(self, "runtime_components", components)
        object.__setattr__(self, "runtime_cut_id", cut_id)
        object.__setattr__(self, "runtime_authority_id", authority_id)
        object.__setattr__(self, "runtime_verifier_id", verifier_id)
        object.__setattr__(self, "runtime_authority_seal", authority_seal)
        object.__setattr__(self, "build_sha", build)
        object.__setattr__(self, "protocol_ref", protocol)

    @property
    def fingerprint(self) -> str:
        material = {
            "schema_version": self.schema_version,
            "replay": {
                "dataset_digest": self.replay.dataset_digest,
                "cursor": self.replay.cursor,
                "clock": self.replay.clock,
            },
            "runtime_components": dict(self.runtime_components),
            "runtime_cut_id": self.runtime_cut_id,
            "runtime_authority_id": self.runtime_authority_id,
            "runtime_verifier_id": self.runtime_verifier_id,
            "runtime_authority_seal": self.runtime_authority_seal,
            "build_sha": self.build_sha,
            "protocol_ref": self.protocol_ref,
        }
        return sha256(
            json.dumps(
                material,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()

    def to_record(self) -> Mapping[str, Any]:
        """Return the canonical persisted checkpoint record.

        This record is a portable integrity envelope over references to the
        actual component authorities; it does not become an authority for their
        underlying state.
        """

        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "replay": MappingProxyType(
                    {
                        "dataset_digest": self.replay.dataset_digest,
                        "cursor": self.replay.cursor,
                        "clock": self.replay.clock,
                    }
                ),
                "runtime_components": MappingProxyType(
                    dict(self.runtime_components)
                ),
                "runtime_cut_id": self.runtime_cut_id,
                "runtime_authority_id": self.runtime_authority_id,
                "runtime_verifier_id": self.runtime_verifier_id,
                "runtime_authority_seal": self.runtime_authority_seal,
                "build_sha": self.build_sha,
                "protocol_ref": self.protocol_ref,
                "fingerprint": self.fingerprint,
            }
        )

    def to_canonical_json(self) -> str:
        return json.dumps(
            _plain_json(self.to_record()),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )

    @classmethod
    def from_canonical_json(cls, document: str) -> "CompositeReplayCheckpoint":
        if not isinstance(document, str):
            raise TypeError("composite replay checkpoint document must be text")
        try:
            decoded = json.loads(document)
        except json.JSONDecodeError as error:
            raise ReplayError(
                "composite replay checkpoint document is not valid JSON"
            ) from error
        if not isinstance(decoded, dict):
            raise ReplayError(
                "composite replay checkpoint document must be a JSON object"
            )
        try:
            canonical = json.dumps(
                decoded,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ReplayError(
                "composite replay checkpoint document contains noncanonical JSON values"
            ) from error
        if canonical != document:
            raise ReplayError(
                "composite replay checkpoint document must use canonical JSON bytes"
            )
        expected_keys = {
            "schema_version",
            "replay",
            "runtime_components",
            "runtime_cut_id",
            "runtime_authority_id",
            "runtime_verifier_id",
            "runtime_authority_seal",
            "build_sha",
            "protocol_ref",
            "fingerprint",
        }
        if set(decoded) != expected_keys:
            raise ReplayError(
                "composite replay checkpoint document has unknown or missing fields"
            )
        replay_value = decoded["replay"]
        if not isinstance(replay_value, dict) or set(replay_value) != {
            "dataset_digest",
            "cursor",
            "clock",
        }:
            raise ReplayError(
                "composite replay checkpoint replay record has invalid fields"
            )
        components = decoded["runtime_components"]
        if not isinstance(components, dict):
            raise ReplayError(
                "composite replay checkpoint runtime_components must be an object"
            )
        persisted_fingerprint = _sha256_hex(
            decoded["fingerprint"],
            field="checkpoint fingerprint",
        )
        checkpoint = cls(
            replay=ReplayCheckpoint(
                dataset_digest=replay_value["dataset_digest"],
                cursor=replay_value["cursor"],
                clock=replay_value["clock"],
            ),
            runtime_components=components,
            runtime_cut_id=decoded["runtime_cut_id"],
            runtime_authority_id=decoded["runtime_authority_id"],
            runtime_verifier_id=decoded["runtime_verifier_id"],
            runtime_authority_seal=decoded["runtime_authority_seal"],
            build_sha=decoded["build_sha"],
            protocol_ref=decoded["protocol_ref"],
            schema_version=decoded["schema_version"],
        )
        if checkpoint.fingerprint != persisted_fingerprint:
            raise ReplayError(
                "composite replay checkpoint fingerprint does not match persisted content"
            )
        return checkpoint


@dataclass(frozen=True)
class ReplayCheckpoint:
    dataset_digest: str
    cursor: int
    clock: str

    def __post_init__(self) -> None:
        if not isinstance(self.dataset_digest, str) or len(self.dataset_digest) != 64:
            raise ReplayError("dataset_digest must be a SHA-256 hex digest")
        try:
            int(self.dataset_digest, 16)
        except ValueError as error:
            raise ReplayError("dataset_digest must be hexadecimal") from error
        if isinstance(self.cursor, bool) or not isinstance(self.cursor, int):
            raise TypeError("cursor must be an integer")
        if self.cursor < 0:
            raise ReplayError("cursor must be non-negative")
        _instant(self.clock, field="clock")


def _event_record(event: ReplayEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "available_at": event.available_at,
        "source_version": event.source_version,
        "payload": _plain_json(event.payload),
    }


def dataset_digest(events: Sequence[ReplayEvent]) -> str:
    encoded = json.dumps(
        [_event_record(event) for event in events],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class CausalReplay:
    """Deterministic replay that exposes only data available by the replay clock."""

    def __init__(
        self,
        events: Iterable[ReplayEvent],
        *,
        start_at: str,
        checkpoint: ReplayCheckpoint | None = None,
    ) -> None:
        records = tuple(events)
        for event in records:
            if not isinstance(event, ReplayEvent):
                raise TypeError("events must contain ReplayEvent values")

        ordered = tuple(
            sorted(
                records,
                key=lambda item: (
                    _instant(item.available_at, field="available_at"),
                    item.sequence,
                ),
            )
        )
        if len({event.sequence for event in ordered}) != len(ordered):
            raise ReplayError("event sequence values must be unique")

        self._events = ordered
        self._digest = dataset_digest(ordered)
        start = _instant(start_at, field="start_at")

        if checkpoint is None:
            self._cursor = 0
            self._clock = start
            return

        if checkpoint.dataset_digest != self._digest:
            raise ReplayError("checkpoint dataset digest does not match replay dataset")
        if checkpoint.cursor > len(self._events):
            raise ReplayError("checkpoint cursor exceeds dataset length")
        restored_clock = _instant(checkpoint.clock, field="clock")
        if restored_clock < start:
            raise ReplayError("checkpoint clock precedes requested start")
        causally_visible = sum(
            _instant(event.available_at, field="available_at") <= restored_clock
            for event in self._events
        )
        if checkpoint.cursor != causally_visible:
            if checkpoint.cursor > causally_visible:
                raise ReplayError(
                    "checkpoint cursor consumes events unavailable at checkpoint clock"
                )
            raise ReplayError(
                "checkpoint cursor omits events already visible at checkpoint clock"
            )
        self._cursor = checkpoint.cursor
        self._clock = restored_clock

    @property
    def clock(self) -> str:
        return self._clock.isoformat().replace("+00:00", "Z")

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def digest(self) -> str:
        return self._digest

    def advance_to(self, instant: str) -> tuple[ReplayEvent, ...]:
        target = _instant(instant, field="instant")
        if target < self._clock:
            raise ReplayError("replay clock cannot move backwards")

        visible: list[ReplayEvent] = []
        while self._cursor < len(self._events):
            event = self._events[self._cursor]
            if _instant(event.available_at, field="available_at") > target:
                break
            visible.append(event)
            self._cursor += 1

        self._clock = target
        return tuple(visible)

    def peek_next(self) -> ReplayEvent | None:
        if self._cursor >= len(self._events):
            return None
        event = self._events[self._cursor]
        if _instant(event.available_at, field="available_at") > self._clock:
            return None
        return event

    def checkpoint(self) -> ReplayCheckpoint:
        return ReplayCheckpoint(
            dataset_digest=self._digest,
            cursor=self._cursor,
            clock=self.clock,
        )

    def remaining(self) -> int:
        return len(self._events) - self._cursor

    def composite_checkpoint(
        self,
        *,
        runtime_state_authority: RuntimeStateAuthority,
        runtime_state_verifier: RuntimeStateVerifier,
        build_sha: str,
        protocol_ref: str,
    ) -> CompositeReplayCheckpoint:
        """Bind source cursor and component state to one authority-issued cut."""

        replay_before = self.checkpoint()
        snapshot = _resolve_runtime_snapshot(
            runtime_state_authority,
            runtime_state_verifier,
        )
        replay_after = self.checkpoint()
        if replay_before != replay_after:
            raise ReplayError(
                "replay cursor or clock changed during runtime snapshot capture"
            )
        if snapshot.replay != replay_before:
            raise ReplayError(
                "runtime state snapshot is not bound to the current replay cut"
            )
        checkpoint_seal = RuntimeStateAuthority.seal_checkpoint(
            runtime_state_authority,
            snapshot,
            build_sha=build_sha,
            protocol_ref=protocol_ref,
        )
        checkpoint = CompositeReplayCheckpoint(
            replay=replay_before,
            runtime_components=snapshot.runtime_components,
            runtime_cut_id=snapshot.cut_id,
            runtime_authority_id=snapshot.authority_id,
            runtime_verifier_id=snapshot.verifier_id,
            runtime_authority_seal=checkpoint_seal,
            build_sha=build_sha,
            protocol_ref=protocol_ref,
        )
        RuntimeStateVerifier.verify_checkpoint_binding(
            runtime_state_verifier,
            checkpoint,
        )
        return checkpoint


def resume_from_composite_checkpoint(
    events: Iterable[ReplayEvent],
    *,
    start_at: str,
    checkpoint: CompositeReplayCheckpoint,
    runtime_state_authority: RuntimeStateAuthority,
    runtime_state_verifier: RuntimeStateVerifier,
    build_sha: str,
    protocol_ref: str,
) -> CausalReplay:
    """Validate one authority-issued common cut before exposing another event."""

    if type(checkpoint) is not CompositeReplayCheckpoint:
        raise TypeError("checkpoint must be the canonical CompositeReplayCheckpoint")
    if type(runtime_state_authority) is not RuntimeStateAuthority:
        raise TypeError(
            "runtime_state_authority must be the canonical RuntimeStateAuthority"
        )
    if type(runtime_state_verifier) is not RuntimeStateVerifier:
        raise TypeError(
            "runtime_state_verifier must be the canonical RuntimeStateVerifier"
        )
    if runtime_state_authority.authority_id != checkpoint.runtime_authority_id:
        raise ReplayError("runtime state authority identity differs from checkpoint")
    if runtime_state_verifier.authority_id != checkpoint.runtime_authority_id:
        raise ReplayError("runtime state verifier authority differs from checkpoint")
    RuntimeStateVerifier.verify_checkpoint_binding(
        runtime_state_verifier,
        checkpoint,
    )
    snapshot = _resolve_runtime_snapshot(
        runtime_state_authority,
        runtime_state_verifier,
    )
    if snapshot.replay != checkpoint.replay:
        raise ReplayError(
            "runtime state snapshot replay cut differs from checkpoint"
        )
    current_seal = RuntimeStateAuthority.seal_checkpoint(
        runtime_state_authority,
        snapshot,
        build_sha=build_sha,
        protocol_ref=protocol_ref,
    )
    current = CompositeReplayCheckpoint(
        replay=snapshot.replay,
        runtime_components=snapshot.runtime_components,
        runtime_cut_id=snapshot.cut_id,
        runtime_authority_id=snapshot.authority_id,
        runtime_verifier_id=snapshot.verifier_id,
        runtime_authority_seal=current_seal,
        build_sha=build_sha,
        protocol_ref=protocol_ref,
    )
    if current.fingerprint != checkpoint.fingerprint:
        raise ReplayError(
            "composite replay checkpoint does not match current runtime state cut"
        )
    return CausalReplay(
        events,
        start_at=start_at,
        checkpoint=checkpoint.replay,
    )
