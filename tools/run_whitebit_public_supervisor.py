"""Standalone, public-only WhiteBIT best-bid/offer collector supervisor.

The process owns exactly one market/state directory, delegates connection and
reconnection to the canonical WhiteBIT WebSocket transport, and publishes only
non-authoritative public market observations.  It has no credential, account,
order, transfer, or trading surface.
"""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import stat
from threading import Event, Lock, Thread
import time
from typing import Any, Callable

from research.autotrade_research.artifacts.durable_publish import (
    atomic_write_json,
    sync_parent_directory,
)
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.artifacts.store import ArtifactStore
from research.autotrade_research.integrations.whitebit_public import _symbol
from research.autotrade_research.integrations.whitebit_websocket import SOURCE_URI
from research.autotrade_research.integrations.whitebit_websocket import (
    WhiteBitPublicWebSocketTransport,
    WhiteBitWebSocketDiagnostic,
    WhiteBitWebSocketMarketFeed,
)
from research.autotrade_research.io.strict_json import strict_json_loads


SCHEMA = "autotrade-whitebit-public-supervisor-v1"
_ACTIVE_LOG = "whitebit-public-diagnostics.jsonl"
_ROTATED_PREFIX = "whitebit-public-diagnostics-"
_ROTATED_SUFFIX = ".jsonl"
_ZERO_DIGEST = "0" * 64
_SAFE_PHASES = frozenset({"STARTING", "RUNNING", "STOPPING", "STOPPED", "FAILED"})
_STATE_FIELDS = frozenset({
    "schema",
    "market",
    "phase",
    "checkpointed_at",
    "observation_count",
    "latest_evidence_ref",
    "public_data_only",
    "trading_authority",
})
_EVIDENCE_FIELDS = frozenset({
    "artifact_id", "sha256", "source_uri", "observed_at", "rights_id",
})


def _utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("supervisor clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _json_line(value: dict[str, Any]) -> bytes:
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


class WhiteBitPublicDiagnosticJournal:
    """Bounded restart-aware heartbeat with a deliberately closed schema."""

    def __init__(
        self,
        root: Path,
        *,
        market: str,
        interval_seconds: float = 1.0,
        max_segment_bytes: int = 512 * 1024,
        max_segments: int = 8,
        monotonic=time.monotonic,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        if type(root) is not type(Path()):
            raise TypeError("diagnostic root must be an exact Path")
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise ValueError("diagnostic root must be an ordinary directory")
        self.market = _symbol(market)
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not 0.1 <= float(interval_seconds) <= 60.0
        ):
            raise ValueError("heartbeat interval must be between 0.1 and 60 seconds")
        if type(max_segment_bytes) is not int or max_segment_bytes < 1024:
            raise ValueError("diagnostic segment size must be at least 1024 bytes")
        if type(max_segments) is not int or not 2 <= max_segments <= 128:
            raise ValueError("diagnostic segment count must be between 2 and 128")
        self.root = root
        self.interval_seconds = float(interval_seconds)
        self.max_segment_bytes = max_segment_bytes
        self.max_segments = max_segments
        self._monotonic = monotonic
        self._clock = clock
        self._guard = Lock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._sequence = 0
        self._previous_digest = _ZERO_DIGEST
        self._phase = "STARTING"
        self._connection_code = "CONNECTING"
        self._attempt = 0
        self._generation = 0
        self._observation_count = 0
        self._last_observed_at: str | None = None
        self.error_code: str | None = None

    @property
    def active_path(self) -> Path:
        return self.root / _ACTIVE_LOG

    def _rotated_paths(self) -> list[Path]:
        result = []
        for path in self.root.glob(_ROTATED_PREFIX + "*" + _ROTATED_SUFFIX):
            middle = path.name[len(_ROTATED_PREFIX) : -len(_ROTATED_SUFFIX)]
            if len(middle) != 20 or not middle.isdigit():
                raise ValueError("unexpected WhiteBIT diagnostic segment name")
            result.append(path)
        return sorted(result)

    @staticmethod
    def _ordinary_file(path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("diagnostic segment must be an ordinary unaliased file")

    def _paths(self) -> list[Path]:
        result = self._rotated_paths()
        if self.active_path.exists() or self.active_path.is_symlink():
            result.append(self.active_path)
        return result

    def _recover(self) -> None:
        paths = self._paths()
        if len(paths) > self.max_segments:
            raise ValueError("diagnostic segment count exceeds configured bound")
        prior_sequence: int | None = None
        prior_digest: str | None = None
        latest_row: dict[str, Any] | None = None
        for path in paths:
            self._ordinary_file(path)
            with path.open("rb") as stream:
                for body in stream:
                    if not body.endswith(b"\n"):
                        raise ValueError("diagnostic segment has an incomplete tail")
                    row = json.loads(body)
                    sequence = row.get("sequence") if type(row) is dict else None
                    if type(sequence) is not int or sequence < 1:
                        raise ValueError("diagnostic sequence is invalid")
                    if (
                        row.get("schema") != SCHEMA
                        or row.get("market") != self.market
                        or row.get("public_data_only") is not True
                        or row.get("trading_authority") is not False
                        or type(row.get("observation_count")) is not int
                        or row["observation_count"] < 0
                        or type(row.get("generation")) is not int
                        or row["generation"] < 0
                        or (
                            row.get("last_observed_at") is not None
                            and type(row["last_observed_at"]) is not str
                        )
                    ):
                        raise ValueError("diagnostic restart state is invalid")
                    if prior_sequence is not None and (
                        sequence != prior_sequence + 1
                        or row.get("previous_sha256") != prior_digest
                    ):
                        raise ValueError("diagnostic sequence or hash chain is broken")
                    prior_sequence = sequence
                    prior_digest = sha256(body).hexdigest()
                    latest_row = row
        if prior_sequence is not None:
            self._sequence = prior_sequence
            self._previous_digest = str(prior_digest)
            self._observation_count = latest_row["observation_count"]
            self._last_observed_at = latest_row["last_observed_at"]
            self._generation = latest_row["generation"]

    def _rotate(self, body_size: int) -> None:
        active = self.active_path
        if active.exists() or active.is_symlink():
            self._ordinary_file(active)
            if active.stat().st_size + body_size > self.max_segment_bytes:
                target = self.root / (
                    _ROTATED_PREFIX + f"{self._sequence:020d}" + _ROTATED_SUFFIX
                )
                if target.exists() or target.is_symlink():
                    raise ValueError("diagnostic rotation target already exists")
                os.replace(active, target)
        rotated = self._rotated_paths()
        while len(rotated) > self.max_segments - 1:
            oldest = rotated.pop(0)
            self._ordinary_file(oldest)
            oldest.unlink()

    def _record(self, event: str) -> dict[str, Any]:
        if event not in {"HEARTBEAT", "TRANSPORT", "OBSERVATION", "CHECKPOINT"}:
            raise ValueError("unsupported diagnostic event")
        return {
            "schema": SCHEMA,
            "recorded_at_utc": _utc(self._clock).isoformat(),
            "sequence": self._sequence + 1,
            "previous_sha256": self._previous_digest,
            "event": event,
            "market": self.market,
            "phase": self._phase,
            "connection_code": self._connection_code,
            "attempt": self._attempt,
            "generation": self._generation,
            "observation_count": self._observation_count,
            "last_observed_at": self._last_observed_at,
            "public_data_only": True,
            "trading_authority": False,
        }

    def _append_locked(self, event: str) -> None:
        body = _json_line(self._record(event))
        if len(body) > self.max_segment_bytes:
            raise ValueError("one diagnostic record exceeds the segment bound")
        self._rotate(len(body))
        active = self.active_path
        if active.exists() or active.is_symlink():
            self._ordinary_file(active)
        with active.open("ab") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        self._sequence += 1
        self._previous_digest = sha256(body).hexdigest()

    def transport(self, diagnostic: WhiteBitWebSocketDiagnostic) -> None:
        if type(diagnostic) is not WhiteBitWebSocketDiagnostic:
            raise TypeError("canonical transport diagnostic is required")
        with self._guard:
            self._connection_code = diagnostic.code
            self._attempt = diagnostic.attempt
            self._generation = diagnostic.generation
            self._append_locked("TRANSPORT")

    def observation(self, observed_at: str, generation: int) -> None:
        if type(observed_at) is not str or type(generation) is not int or generation < 1:
            raise ValueError("canonical observation identity is required")
        with self._guard:
            self._phase = "RUNNING"
            self._connection_code = "SUBSCRIBED"
            self._generation = generation
            self._observation_count += 1
            self._last_observed_at = observed_at
            self._append_locked("OBSERVATION")

    def checkpoint(self, phase: str) -> None:
        if phase not in _SAFE_PHASES:
            raise ValueError("unsupported supervisor phase")
        with self._guard:
            self._phase = phase
            self._append_locked("CHECKPOINT")

    def _run(self) -> None:
        next_tick = float(self._monotonic()) + self.interval_seconds
        while not self._stop.is_set():
            if self._stop.wait(max(0.0, next_tick - float(self._monotonic()))):
                return
            try:
                with self._guard:
                    self._append_locked("HEARTBEAT")
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                # Persist only a bounded local error category, never exception text.
                self.error_code = "DIAGNOSTIC_" + type(error).__name__.upper()
                return
            next_tick += self.interval_seconds
            if next_tick <= float(self._monotonic()):
                next_tick = float(self._monotonic()) + self.interval_seconds

    def start(self) -> None:
        with self._guard:
            if self._thread is not None:
                raise RuntimeError("diagnostic journal is already started")
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink() or not self.root.is_dir():
                raise ValueError("diagnostic root must be an ordinary directory")
            self._recover()
            self._append_locked("CHECKPOINT")
            self._stop.clear()
            self._thread = Thread(
                target=self._run,
                name="autotrade-whitebit-public-heartbeat",
                daemon=False,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2.0, self.interval_seconds + 1.0))
            if thread.is_alive():
                raise RuntimeError("WhiteBIT diagnostic heartbeat could not stop")


def _snapshot(update: Any) -> dict[str, Any]:
    payload = update.payload
    evidence = update.raw_evidence_ref
    expected_payload = {"bid_price", "bid_quantity", "ask_price", "ask_quantity"}
    expected_evidence = {
        "artifact_id",
        "sha256",
        "source_uri",
        "observed_at",
        "rights_id",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_payload:
        raise ValueError("WhiteBIT update payload differs from the public BBO contract")
    if not isinstance(evidence, Mapping) or set(evidence) != expected_evidence:
        raise ValueError("WhiteBIT update evidence differs from the public contract")
    return {
        "schema": SCHEMA,
        "market": update.provider_symbol,
        "venue": update.venue_id,
        "bid_price": payload["bid_price"],
        "bid_quantity": payload["bid_quantity"],
        "ask_price": payload["ask_price"],
        "ask_quantity": payload["ask_quantity"],
        "source_event_at": update.source_event_at.isoformat(),
        "available_at": update.available_at.isoformat(),
        "published_at": update.ingested_at.isoformat(),
        "update_id": update.source_sequence,
        "stream_generation": update.stream_generation,
        "raw_evidence_ref": dict(evidence),
        "public_data_only": True,
        "trading_authority": False,
    }


def _checkpoint(
    path: Path,
    *,
    market: str,
    phase: str,
    observation_count: int,
    latest: dict[str, Any] | None,
    clock: Callable[[], datetime],
) -> None:
    atomic_write_json(
        path,
        {
            "schema": SCHEMA,
            "market": market,
            "phase": phase,
            "checkpointed_at": _utc(clock).isoformat(),
            "observation_count": observation_count,
            "latest_evidence_ref": None if latest is None else latest["raw_evidence_ref"],
            "public_data_only": True,
            "trading_authority": False,
        },
    )


def _read_restart_state(
    state_dir: Path,
    *,
    market: str,
    artifacts: ArtifactStore,
) -> tuple[int, dict[str, Any] | None]:
    """Recover only an authenticated, internally coherent supervisor cut."""

    state_path = state_dir / "supervisor-state.json"
    if not state_path.exists() and not state_path.is_symlink():
        return 0, None
    if state_path.is_symlink() or not state_path.is_file():
        raise ValueError("supervisor checkpoint must be an ordinary file")
    state = strict_json_loads(state_path.read_text(encoding="utf-8"))
    if (
        type(state) is not dict
        or frozenset(state) != _STATE_FIELDS
        or state.get("schema") != SCHEMA
        or state.get("market") != market
        or state.get("phase") not in _SAFE_PHASES
        or type(state.get("observation_count")) is not int
        or state["observation_count"] < 0
        or state.get("public_data_only") is not True
        or state.get("trading_authority") is not False
    ):
        raise ValueError("supervisor checkpoint is not canonical")
    checkpointed_at = datetime.fromisoformat(state["checkpointed_at"])
    if checkpointed_at.tzinfo is None:
        raise ValueError("supervisor checkpoint timestamp must be timezone-aware")

    evidence = state["latest_evidence_ref"]
    if state["observation_count"] == 0:
        if evidence is not None:
            raise ValueError("empty supervisor checkpoint references evidence")
        return 0, None
    if type(evidence) is not dict or frozenset(evidence) != _EVIDENCE_FIELDS:
        raise ValueError("supervisor checkpoint evidence is not canonical")
    latest_path = state_dir / "latest-market.json"
    if latest_path.is_symlink() or not latest_path.is_file():
        raise ValueError("supervisor latest snapshot is unavailable")
    latest = strict_json_loads(latest_path.read_text(encoding="utf-8"))
    if (
        type(latest) is not dict
        or latest.get("schema") != SCHEMA
        or latest.get("market") != market
        or latest.get("raw_evidence_ref") != evidence
        or latest.get("public_data_only") is not True
        or latest.get("trading_authority") is not False
    ):
        raise ValueError("supervisor restart snapshot is not coherent")
    manifest, _raw = artifacts.read_authenticated_snapshot(evidence["artifact_id"])
    if manifest.get("sha256") != evidence["sha256"]:
        raise ValueError("supervisor restart evidence digest changed")
    return state["observation_count"], latest


def _ordinary_owned_file(path: Path, *, parent: Path) -> None:
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("artifact quota directory must be ordinary")
    if path.parent.resolve(strict=True) != parent.resolve(strict=True):
        raise ValueError("artifact quota path escapes its owned directory")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("artifact quota entry must be an ordinary unaliased file")


class WhiteBitArtifactQuota:
    """Bounded raw-frame retention with one full scan only at restart."""

    def __init__(self, artifacts: ArtifactStore, *, market: str, maximum: int):
        if type(artifacts) is not ArtifactStore:
            raise TypeError("artifact quota requires the canonical ArtifactStore")
        if type(maximum) is not int or not 1 <= maximum <= 1_000_000:
            raise ValueError("artifact quota must be between 1 and 1000000")
        self.artifacts = artifacts
        self.market = market
        self.maximum = maximum
        self._entries: list[tuple[str, str, str]] = []

    def _entry(self, artifact_id: str) -> tuple[str, str, str]:
        manifest, _raw = self.artifacts.read_authenticated_snapshot(artifact_id)
        metadata = manifest.get("metadata")
        if (
            manifest.get("media_type") != "application/json"
            or manifest.get("source_refs") != [SOURCE_URI]
            or type(metadata) is not dict
            or metadata.get("provider") != "WHITEBIT"
            or metadata.get("market") != self.market
        ):
            raise ValueError("artifact quota encountered foreign evidence")
        return manifest["created_at"], artifact_id, manifest["sha256"]

    def _remove_manifest(self, entry: tuple[str, str, str]) -> None:
        _created_at, artifact_id, _digest = entry
        path = self.artifacts.manifests / (artifact_id + ".json")
        _ordinary_owned_file(path, parent=self.artifacts.manifests)
        path.unlink()
        sync_parent_directory(path)

    def _remove_object_if_unreferenced(self, digest_value: str) -> None:
        if any(item[2] == digest_value for item in self._entries):
            return
        digest = digest_value.removeprefix("sha256:")
        path = self.artifacts._object_path(digest)
        if not path.exists() and not path.is_symlink():
            return
        _ordinary_owned_file(path, parent=path.parent)
        path.unlink()
        sync_parent_directory(path)

    def recover(self, *, protected_artifact_id: str | None) -> None:
        """Validate the retained set, repair crash orphans, and apply the cap."""

        with ResourceLock(self.artifacts.lock_path):
            entries: list[tuple[str, str, str]] = []
            for path in sorted(self.artifacts.manifests.glob("*.json")):
                _ordinary_owned_file(path, parent=self.artifacts.manifests)
                entries.append(self._entry(path.stem))
            by_newest = sorted(entries, reverse=True)
            retained: set[str] = set()
            if protected_artifact_id is not None:
                if protected_artifact_id not in {item[1] for item in entries}:
                    raise ValueError("checkpoint evidence is absent from artifact store")
                retained.add(protected_artifact_id)
            for _created_at, artifact_id, _digest in by_newest:
                if len(retained) >= self.maximum:
                    break
                retained.add(artifact_id)
            for entry in entries:
                if entry[1] not in retained:
                    self._remove_manifest(entry)
            self._entries = sorted(
                (entry for entry in entries if entry[1] in retained)
            )
            referenced = {entry[2].removeprefix("sha256:") for entry in self._entries}
            for path in sorted(self.artifacts.objects.glob("*/*")):
                if path.name in referenced:
                    continue
                expected = self.artifacts._object_path(path.name)
                if path != expected:
                    raise ValueError("artifact quota encountered a non-canonical object")
                _ordinary_owned_file(path, parent=path.parent)
                path.unlink()
                sync_parent_directory(path)

    def admit(self, artifact_id: str) -> None:
        """Admit one just-published frame and evict at most one old frame."""

        with ResourceLock(self.artifacts.lock_path):
            if artifact_id in {item[1] for item in self._entries}:
                raise ValueError("artifact quota received a duplicate identity")
            self._entries.append(self._entry(artifact_id))
            if len(self._entries) <= self.maximum:
                return
            removed = self._entries.pop(0)
            self._remove_manifest(removed)
            self._remove_object_if_unreferenced(removed[2])


async def supervise(
    *,
    market: str,
    state_dir: Path,
    connector=None,
    stop: asyncio.Event | None = None,
    install_signals: bool = True,
    heartbeat_seconds: float = 1.0,
    max_segment_bytes: int = 512 * 1024,
    max_segments: int = 8,
    checkpoint_seconds: float = 30.0,
    max_artifacts: int = 4096,
    clock=lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Collect until stopped and leave a durable terminal checkpoint."""
    if type(state_dir) is not type(Path()):
        raise TypeError("state_dir must be an exact Path")
    canonical_market = _symbol(market)
    if state_dir.exists() and state_dir.is_symlink():
        raise ValueError("state directory must not be redirected")
    state_dir = state_dir.resolve(strict=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise ValueError("state directory must be an ordinary directory")
    stop_event = stop or asyncio.Event()
    if type(stop_event) is not asyncio.Event:
        raise TypeError("stop must be an exact asyncio.Event")
    if (
        isinstance(checkpoint_seconds, bool)
        or not isinstance(checkpoint_seconds, (int, float))
        or not 0.1 <= float(checkpoint_seconds) <= 3600.0
    ):
        raise ValueError("checkpoint interval must be between 0.1 and 3600 seconds")
    if type(max_artifacts) is not int or not 1 <= max_artifacts <= 1_000_000:
        raise ValueError("artifact quota must be between 1 and 1000000")

    loop = asyncio.get_running_loop()
    previous_handlers: dict[signal.Signals, Any] = {}
    if install_signals:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda _number, _frame, event=stop_event: loop.call_soon_threadsafe(event.set))

    artifacts = ArtifactStore(state_dir / "artifacts")
    artifact_quota = WhiteBitArtifactQuota(
        artifacts,
        market=canonical_market,
        maximum=max_artifacts,
    )
    observations = 0
    latest: dict[str, Any] | None = None
    phase = "FAILED"
    journal = WhiteBitPublicDiagnosticJournal(
        state_dir / "diagnostics",
        market=canonical_market,
        interval_seconds=heartbeat_seconds,
        max_segment_bytes=max_segment_bytes,
        max_segments=max_segments,
        clock=clock,
    )
    try:
        with ResourceLock(state_dir / "whitebit-public-supervisor.lock"):
            observations, latest = _read_restart_state(
                state_dir,
                market=canonical_market,
                artifacts=artifacts,
            )
            artifact_quota.recover(
                protected_artifact_id=(
                    None
                    if latest is None
                    else latest["raw_evidence_ref"]["artifact_id"]
                ),
            )
            journal.start()
            _checkpoint(
                state_dir / "supervisor-state.json",
                market=canonical_market,
                phase="STARTING",
                observation_count=observations,
                latest=latest,
                clock=clock,
            )
            transport = WhiteBitPublicWebSocketTransport(
                market=canonical_market,
                connector=connector,
                diagnostic=journal.transport,
            )
            feed = WhiteBitWebSocketMarketFeed(
                transport=transport,
                artifacts=artifacts,
            )

            def publish(update: Any) -> None:
                nonlocal latest, observations
                latest = _snapshot(update)
                atomic_write_json(state_dir / "latest-market.json", latest)
                observations += 1
                journal.observation(latest["available_at"], latest["stream_generation"])
                _checkpoint(
                    state_dir / "supervisor-state.json",
                    market=canonical_market,
                    phase="RUNNING",
                    observation_count=observations,
                    latest=latest,
                    clock=clock,
                )
                artifact_quota.admit(latest["raw_evidence_ref"]["artifact_id"])

            async def periodic_checkpoint() -> None:
                while not stop_event.is_set():
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(), timeout=float(checkpoint_seconds)
                        )
                    except TimeoutError:
                        _checkpoint(
                            state_dir / "supervisor-state.json",
                            market=canonical_market,
                            phase="RUNNING" if latest is not None else "STARTING",
                            observation_count=observations,
                            latest=latest,
                            clock=clock,
                        )

            async def collect_sequentially() -> None:
                async for observation in transport.observations(stop_event):
                    publish(feed._commit(observation))

            checkpoint_task = asyncio.create_task(periodic_checkpoint())
            try:
                await collect_sequentially()
            finally:
                checkpoint_task.cancel()
                await asyncio.gather(checkpoint_task, return_exceptions=True)
            phase = "STOPPED"
            journal.checkpoint("STOPPING")
            _checkpoint(
                state_dir / "supervisor-state.json",
                market=canonical_market,
                phase=phase,
                observation_count=observations,
                latest=latest,
                clock=clock,
            )
            journal.checkpoint(phase)
    except BaseException:
        if journal._thread is not None:
            try:
                journal.checkpoint("FAILED")
                _checkpoint(
                    state_dir / "supervisor-state.json",
                    market=canonical_market,
                    phase="FAILED",
                    observation_count=observations,
                    latest=latest,
                    clock=clock,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        raise
    finally:
        journal.stop()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return {
        "phase": phase,
        "market": canonical_market,
        "observation_count": observations,
        "latest_evidence_ref": None if latest is None else latest["raw_evidence_ref"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", required=True, help="WhiteBIT public market, for example BTC_USDT")
    parser.add_argument("--state-dir", required=True, help="Persistent local collector state directory")
    parser.add_argument("--checkpoint-seconds", type=float, default=30.0)
    parser.add_argument("--max-artifacts", type=int, default=4096)
    args = parser.parse_args(argv)
    try:
        outcome = asyncio.run(
            supervise(
                market=args.market,
                state_dir=Path(args.state_dir),
                checkpoint_seconds=args.checkpoint_seconds,
                max_artifacts=args.max_artifacts,
            )
        )
        print(json.dumps(outcome, sort_keys=True), flush=True)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        # Never print provider/library exception text: it can contain local
        # proxy details or other sensitive material.
        print(
            json.dumps(
                {"phase": "FAILED", "error_code": type(error).__name__.upper()},
                sort_keys=True,
            ),
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
