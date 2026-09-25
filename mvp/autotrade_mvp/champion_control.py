"""Durable fail-closed champion routing control.

This module changes only future model/strategy routing. It never expands trading
authority and never liquidates or silently reassigns management of existing
positions. Promotion and rollback reuse the canonical JournalStore aggregate
version as a compare-and-swap generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Iterable

from .persistence import JournalStore, payload_digest


class ChampionControlError(ValueError):
    pass


class ChampionConflict(ChampionControlError):
    pass


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChampionControlError(f"{name} is required")
    return value.strip()


def _sha256(value: object, *, name: str) -> str:
    text = _text(value, name=name).lower()
    if text.startswith("sha256:"):
        text = text[7:]
    if len(text) != 64:
        raise ChampionControlError(f"{name} must be a SHA-256 digest")
    try:
        int(text, 16)
    except ValueError as error:
        raise ChampionControlError(f"{name} must be hexadecimal") from error
    return "sha256:" + text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: str) -> str:
    material = "|".join(parts).encode("utf-8")
    return prefix + sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    decision_id: str
    scope_id: str
    candidate_version: str
    prior_version: str
    result_refs: tuple[str, ...]
    envelope_digest: str
    authority_policy_digest: str
    independent_gate_signer: str
    gate_verdict: str
    rollback_target: str
    source_sha256: str

    def __post_init__(self) -> None:
        for field in (
            "decision_id",
            "scope_id",
            "candidate_version",
            "prior_version",
            "independent_gate_signer",
            "rollback_target",
        ):
            object.__setattr__(
                self,
                field,
                _text(getattr(self, field), name=field),
            )
        refs = tuple(_text(item, name="result_ref") for item in self.result_refs)
        if not refs:
            raise ChampionControlError("promotion requires evaluation result references")
        if len(set(refs)) != len(refs):
            raise ChampionControlError("promotion result references must be unique")
        object.__setattr__(self, "result_refs", refs)
        object.__setattr__(
            self,
            "envelope_digest",
            _sha256(self.envelope_digest, name="envelope_digest"),
        )
        object.__setattr__(
            self,
            "authority_policy_digest",
            _sha256(self.authority_policy_digest, name="authority_policy_digest"),
        )
        object.__setattr__(
            self,
            "source_sha256",
            _sha256(self.source_sha256, name="source_sha256"),
        )
        verdict = _text(self.gate_verdict, name="gate_verdict").upper()
        if verdict not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise ChampionControlError("unsupported gate_verdict")
        object.__setattr__(self, "gate_verdict", verdict)
        if self.candidate_version == self.prior_version:
            raise ChampionControlError("candidate must differ from prior champion")


@dataclass(frozen=True, slots=True)
class ChampionSnapshot:
    scope_id: str
    generation: int
    future_route_version: str
    existing_position_management_version: str
    retained_versions: tuple[str, ...]
    authority_policy_digest: str
    last_decision_id: str | None

    @property
    def existing_positions_need_explicit_migration(self) -> bool:
        return self.existing_position_management_version != self.future_route_version


class DurableChampionControl:
    _AGGREGATE = "champion_control"

    def __init__(
        self,
        *,
        journal: JournalStore,
        scope_id: str,
        initial_champion_version: str,
        authority_policy_digest: str,
    ) -> None:
        if not isinstance(journal, JournalStore):
            raise TypeError("journal must be JournalStore")
        self.journal = journal
        self.scope_id = _text(scope_id, name="scope_id")
        self.initial_champion_version = _text(
            initial_champion_version,
            name="initial_champion_version",
        )
        self.authority_policy_digest = _sha256(
            authority_policy_digest,
            name="authority_policy_digest",
        )
        existing_events = self.journal.load_events(self._AGGREGATE, self.scope_id)
        if not existing_events:
            payload = {
                "initial_champion_version": self.initial_champion_version,
                "authority_policy_digest": self.authority_policy_digest,
            }
            envelope = self._event(
                event_type="ChampionInitialized",
                aggregate_version=1,
                payload=payload,
                event_id=_stable_id(
                    "champion-init-",
                    self.scope_id,
                    self.initial_champion_version,
                    self.authority_policy_digest,
                ),
            )
            try:
                self.journal.append_event(envelope)
            except ValueError:
                pass
            existing_events = self.journal.load_events(self._AGGREGATE, self.scope_id)
        first_payload = existing_events[0]["payload"]
        durable_initial = _text(
            first_payload["initial_champion_version"],
            name="initial_champion_version",
        )
        if durable_initial != self.initial_champion_version:
            raise ChampionControlError(
                "initial champion conflicts with durable champion control"
            )
        snapshot = self.snapshot()
        if snapshot.authority_policy_digest != self.authority_policy_digest:
            raise ChampionControlError(
                "authority policy conflicts with durable champion control"
            )
    def _event(
        self,
        *,
        event_type: str,
        aggregate_version: int,
        payload: dict[str, object],
        event_id: str,
    ) -> dict[str, object]:
        return {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": self._AGGREGATE,
            "aggregate_id": self.scope_id,
            "aggregate_version": aggregate_version,
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _utc_now(),
        }

    def snapshot(self) -> ChampionSnapshot:
        events = self.journal.load_events(self._AGGREGATE, self.scope_id)
        if not events or events[0]["event_type"] != "ChampionInitialized":
            raise ChampionControlError("champion initialization is missing")
        first = events[0]["payload"]
        future = _text(
            first["initial_champion_version"],
            name="initial_champion_version",
        )
        management = future
        policy = _sha256(
            first["authority_policy_digest"],
            name="authority_policy_digest",
        )
        retained: list[str] = [future]
        last_decision: str | None = None

        for expected, event in enumerate(events, start=1):
            if event["aggregate_version"] != expected:
                raise ChampionControlError(
                    "champion event sequence is not contiguous"
                )
            payload = event["payload"]
            event_type = event["event_type"]
            if event_type == "ChampionInitialized":
                if expected != 1:
                    raise ChampionControlError(
                        "champion can only be initialized once"
                    )
                continue
            if event_type == "ChampionPromoted":
                prior = _text(payload["prior_version"], name="prior_version")
                candidate = _text(
                    payload["candidate_version"],
                    name="candidate_version",
                )
                if prior != future:
                    raise ChampionControlError(
                        "promotion history prior version does not match route"
                    )
                future = candidate
                if candidate not in retained:
                    retained.append(candidate)
                last_decision = _text(
                    payload["decision_id"],
                    name="decision_id",
                )
                continue
            if event_type == "ChampionRolledBack":
                target = _text(payload["target_version"], name="target_version")
                if target not in retained:
                    raise ChampionControlError(
                        "rollback target was never retained"
                    )
                future = target
                last_decision = _text(
                    payload["rollback_id"],
                    name="rollback_id",
                )
                continue
            if event_type == "PositionManagementMigrated":
                target = _text(payload["target_version"], name="target_version")
                if target not in retained:
                    raise ChampionControlError(
                        "position-management target was never retained"
                    )
                management = target
                continue
            raise ChampionControlError(
                f"unsupported champion event type: {event_type}"
            )

        return ChampionSnapshot(
            scope_id=self.scope_id,
            generation=len(events),
            future_route_version=future,
            existing_position_management_version=management,
            retained_versions=tuple(retained),
            authority_policy_digest=policy,
            last_decision_id=last_decision,
        )

    def promote(
        self,
        decision: PromotionDecision,
        *,
        expected_generation: int,
    ) -> ChampionSnapshot:
        if not isinstance(decision, PromotionDecision):
            raise TypeError("decision must be PromotionDecision")
        if (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation < 1
        ):
            raise ChampionControlError(
                "expected_generation must be a positive integer"
            )
        event_id = _stable_id(
            "champion-promote-",
            self.scope_id,
            decision.decision_id,
        )
        existing = self.journal.get_event(event_id)
        if existing is not None:
            payload = existing["payload"]
            if (
                existing["event_type"] != "ChampionPromoted"
                or payload.get("candidate_version") != decision.candidate_version
                or payload.get("prior_version") != decision.prior_version
                or payload.get("source_sha256") != decision.source_sha256
                or payload.get("scope_id") != decision.scope_id
                or tuple(payload.get("result_refs", ())) != decision.result_refs
                or payload.get("envelope_digest") != decision.envelope_digest
                or payload.get("authority_policy_digest") != decision.authority_policy_digest
                or payload.get("independent_gate_signer") != decision.independent_gate_signer
                or payload.get("gate_verdict") != decision.gate_verdict
                or payload.get("rollback_target") != decision.rollback_target
            ):
                raise ChampionConflict(
                    "promotion decision identity conflicts with durable event"
                )
            return self.snapshot()

        state = self.snapshot()
        if state.generation != expected_generation:
            raise ChampionConflict("champion generation changed")
        if decision.scope_id != self.scope_id:
            raise ChampionControlError("promotion scope does not match control")
        if decision.gate_verdict != "PASS":
            raise ChampionControlError("only independent PASS may be promoted")
        if decision.prior_version != state.future_route_version:
            raise ChampionConflict("promotion prior version is stale")
        if decision.rollback_target != decision.prior_version:
            raise ChampionControlError(
                "promotion rollback target must retain the prior champion"
            )
        if decision.authority_policy_digest != state.authority_policy_digest:
            raise ChampionControlError(
                "promotion cannot change hard authority policy"
            )

        version = state.generation + 1
        payload = {
            "decision_id": decision.decision_id,
            "scope_id": decision.scope_id,
            "candidate_version": decision.candidate_version,
            "prior_version": decision.prior_version,
            "result_refs": list(decision.result_refs),
            "envelope_digest": decision.envelope_digest,
            "authority_policy_digest": decision.authority_policy_digest,
            "independent_gate_signer": decision.independent_gate_signer,
            "gate_verdict": decision.gate_verdict,
            "rollback_target": decision.rollback_target,
            "source_sha256": decision.source_sha256,
        }
        envelope = self._event(
            event_type="ChampionPromoted",
            aggregate_version=version,
            payload=payload,
            event_id=event_id,
        )
        try:
            _, inserted, _ = self.journal.commit_command(
                command_id=_stable_id(
                    "champion-command-",
                    self.scope_id,
                    decision.decision_id,
                ),
                idempotency_key=f"champion:{self.scope_id}:promote:{decision.decision_id}",
                request={
                    "expected_generation": expected_generation,
                    "decision": payload,
                },
                result={
                    "future_route_version": decision.candidate_version,
                    "generation": version,
                },
                state_version=version,
                events=[(envelope, None)],
            )
        except ValueError as error:
            if "aggregate_version must be" in str(error):
                raise ChampionConflict("concurrent promotion won the generation") from error
            raise
        if not inserted:
            replayed = self.snapshot()
            if replayed.future_route_version != decision.candidate_version:
                raise ChampionConflict(
                    "idempotent promotion does not match durable route"
                )
            return replayed
        return self.snapshot()

    def rollback(
        self,
        *,
        rollback_id: str,
        target_version: str,
        expected_generation: int,
        reason_ref: str,
    ) -> ChampionSnapshot:
        rollback_id = _text(rollback_id, name="rollback_id")
        target = _text(target_version, name="target_version")
        reason = _text(reason_ref, name="reason_ref")
        event_id = _stable_id(
            "champion-rollback-",
            self.scope_id,
            rollback_id,
        )
        existing = self.journal.get_event(event_id)
        if existing is not None:
            payload = existing["payload"]
            if (
                existing["event_type"] != "ChampionRolledBack"
                or payload.get("target_version") != target
                or payload.get("reason_ref") != reason
            ):
                raise ChampionConflict(
                    "rollback identity conflicts with durable event"
                )
            return self.snapshot()
        state = self.snapshot()
        if state.generation != expected_generation:
            raise ChampionConflict("champion generation changed")
        if target not in state.retained_versions:
            raise ChampionControlError(
                "rollback target is not a retained champion artifact"
            )
        if target == state.future_route_version:
            raise ChampionControlError("rollback target is already active")
        version = state.generation + 1
        payload = {
            "rollback_id": rollback_id,
            "from_version": state.future_route_version,
            "target_version": target,
            "reason_ref": reason,
        }
        envelope = self._event(
            event_type="ChampionRolledBack",
            aggregate_version=version,
            payload=payload,
            event_id=event_id,
        )
        try:
            self.journal.commit_command(
                command_id=_stable_id(
                    "champion-command-rollback-",
                    self.scope_id,
                    rollback_id,
                ),
                idempotency_key=f"champion:{self.scope_id}:rollback:{rollback_id}",
                request={
                    "expected_generation": expected_generation,
                    **payload,
                },
                result={
                    "future_route_version": target,
                    "generation": version,
                },
                state_version=version,
                events=[(envelope, None)],
            )
        except ValueError as error:
            if "aggregate_version must be" in str(error):
                raise ChampionConflict("concurrent transition won the generation") from error
            raise
        return self.snapshot()

    def migrate_existing_position_management(
        self,
        *,
        migration_id: str,
        target_version: str,
        expected_generation: int,
        compatibility_evidence_refs: Iterable[str],
    ) -> ChampionSnapshot:
        migration_id = _text(migration_id, name="migration_id")
        target = _text(target_version, name="target_version")
        refs = tuple(
            _text(item, name="compatibility_evidence_ref")
            for item in compatibility_evidence_refs
        )
        if not refs:
            raise ChampionControlError(
                "position-management migration requires compatibility evidence"
            )
        event_id = _stable_id(
            "champion-position-migration-",
            self.scope_id,
            migration_id,
        )
        existing = self.journal.get_event(event_id)
        if existing is not None:
            payload = existing["payload"]
            if (
                existing["event_type"] != "PositionManagementMigrated"
                or payload.get("target_version") != target
                or tuple(payload.get("compatibility_evidence_refs", ())) != refs
            ):
                raise ChampionConflict(
                    "position-management migration identity conflicts with durable event"
                )
            return self.snapshot()
        state = self.snapshot()
        if state.generation != expected_generation:
            raise ChampionConflict("champion generation changed")
        if target not in state.retained_versions:
            raise ChampionControlError(
                "position-management target is not retained"
            )
        version = state.generation + 1
        payload = {
            "migration_id": migration_id,
            "target_version": target,
            "compatibility_evidence_refs": list(refs),
        }
        envelope = self._event(
            event_type="PositionManagementMigrated",
            aggregate_version=version,
            payload=payload,
            event_id=event_id,
        )
        try:
            self.journal.commit_command(
                command_id=_stable_id(
                    "champion-command-position-",
                    self.scope_id,
                    migration_id,
                ),
                idempotency_key=f"champion:{self.scope_id}:position:{migration_id}",
                request={
                    "expected_generation": expected_generation,
                    **payload,
                },
                result={
                    "position_management_version": target,
                    "generation": version,
                },
                state_version=version,
                events=[(envelope, None)],
            )
        except ValueError as error:
            if "aggregate_version must be" in str(error):
                raise ChampionConflict("concurrent transition won the generation") from error
            raise
        return self.snapshot()
