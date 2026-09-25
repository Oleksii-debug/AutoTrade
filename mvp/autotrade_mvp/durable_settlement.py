"""Durable journal-backed settlement provenance over the canonical SettlementBook.

This module persists settlement obligations and provider settlement evidence in
the existing JournalStore.  It is not a second cash ledger: settled/unsettled
cash is always re-projected from the canonical EconomicBook plus this immutable
provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)
from research.autotrade_research.io.strict_json import strict_json_loads

from .persistence import JournalStore, canonical_json, payload_digest
from .settlement import (
    SettlementAccountScope,
    SettlementBook,
    SettlementConflict,
    SettlementEvidence,
    SettlementObligation,
    SettlementRuleBinding,
)


_AGGREGATE_TYPE = "settlement_book"
_REGISTER_EVENT = "SettlementObligationsRegistered"
_SETTLE_EVENT = "SettlementEvidenceApplied"
_ACTOR = "settlement-provenance"
SETTLEMENT_EVIDENCE_MEDIA_TYPE = "application/vnd.autotrade.settlement-evidence+json"
SETTLEMENT_EVIDENCE_TYPE = "AUTOTRADE_SETTLEMENT_EVIDENCE"
SETTLEMENT_EVIDENCE_SCHEMA_VERSION = 1


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _artifact_ref(value: object, *, name: str) -> tuple[str, str, str]:
    if not isinstance(value, str) or not value.strip():
        raise SettlementConflict(
            f"{name} requires immutable ArtifactStore evidence"
        )
    reference = value.strip()
    marker = "@sha256:"
    if not reference.startswith("artifact:") or marker not in reference:
        raise SettlementConflict(
            f"{name} must bind artifact UUID and SHA-256 digest"
        )
    artifact_id, digest = reference[len("artifact:"):].split(marker, 1)
    try:
        artifact_id = str(UUID(artifact_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise SettlementConflict(f"{name} artifact identity must be UUID") from error
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise SettlementConflict(f"{name} must use canonical lowercase SHA-256")
    canonical = f"artifact:{artifact_id}@sha256:{digest}"
    if canonical != reference:
        raise SettlementConflict(f"{name} must be canonical")
    return artifact_id, digest, canonical


def _verify_artifact(
    artifact_store: ArtifactStore,
    *,
    evidence_ref: str,
    expected_receipt: Mapping[str, object],
    expected_metadata: Mapping[str, object],
    name: str,
) -> str:
    if not isinstance(artifact_store, ArtifactStore):
        raise SettlementConflict(f"{name} requires trusted ArtifactStore")
    artifact_id, digest, canonical_ref = _artifact_ref(
        evidence_ref, name=f"{name} evidence_ref"
    )
    try:
        manifest = artifact_store.load_manifest(artifact_id)
        manifest_hash = manifest.get("manifest_hash")
        if (
            not isinstance(manifest_hash, str)
            or not manifest_hash.startswith("sha256:")
            or len(manifest_hash) != 71
        ):
            raise ArtifactIntegrityError("evidence manifest lacks integrity binding")
        if manifest.get("sha256") != f"sha256:{digest}":
            raise ArtifactIntegrityError("evidence digest does not match manifest")
        if manifest.get("media_type") != SETTLEMENT_EVIDENCE_MEDIA_TYPE:
            raise ArtifactIntegrityError("unsupported settlement evidence media type")
        if manifest.get("metadata") != dict(expected_metadata):
            raise ArtifactIntegrityError("settlement evidence metadata mismatch")
        rights = manifest.get("rights")
        if not isinstance(rights, dict) or rights.get("storage") is not True:
            raise ArtifactIntegrityError("settlement evidence lacks storage provenance")
        raw = artifact_store.read_bytes(artifact_id)
        parsed = strict_json_loads(raw.decode("utf-8"))
    except (
        ArtifactIntegrityError,
        FileNotFoundError,
        UnicodeError,
        ValueError,
        TypeError,
    ) as error:
        raise SettlementConflict(f"{name} artifact verification failed") from error
    expected = dict(expected_receipt)
    if parsed != expected:
        raise SettlementConflict(
            f"{name} artifact differs from bound financial semantics"
        )
    if raw != canonical_json(expected).encode("utf-8"):
        raise SettlementConflict(f"{name} artifact must use canonical JSON bytes")
    return canonical_ref


def _scope_id(scope: SettlementAccountScope) -> str:
    material = canonical_json(
        [scope.provider_id, scope.account_id, scope.environment, "settlement-book"]
    )
    return str(uuid5(NAMESPACE_URL, "settlement-book:" + material))


def _event_id(scope: SettlementAccountScope, kind: str, identity: object) -> str:
    material = canonical_json(
        [scope.provider_id, scope.account_id, scope.environment, kind, identity]
    )
    return str(uuid5(NAMESPACE_URL, "settlement-event:" + material))


def _scope_payload(scope: SettlementAccountScope) -> dict[str, str]:
    return {
        "provider_id": scope.provider_id,
        "account_id": scope.account_id,
        "environment": scope.environment,
    }


def _rule_payload(rule: SettlementRuleBinding) -> dict[str, object]:
    return {
        "rule_id": rule.rule_id,
        "rule_version": rule.rule_version,
        "scope": _scope_payload(rule.scope),
        "instrument_version": rule.instrument_version,
        "settlement_currency": rule.settlement_currency,
        "effective_from": rule.effective_from.isoformat(),
        "effective_to": (
            None if rule.effective_to is None else rule.effective_to.isoformat()
        ),
        "evidence_refs": list(rule.evidence_refs),
        "digest": rule.digest,
    }


def settlement_rule_evidence_receipt(
    rule: SettlementRuleBinding,
    *,
    trade_date: date,
    expected_settlement_date: date,
) -> dict[str, object]:
    if not isinstance(rule, SettlementRuleBinding):
        raise TypeError("rule must be SettlementRuleBinding")
    if type(trade_date) is not date or type(expected_settlement_date) is not date:
        raise TypeError("trade_date and expected_settlement_date must be dates")
    return {
        "schema_version": SETTLEMENT_EVIDENCE_SCHEMA_VERSION,
        "evidence_type": SETTLEMENT_EVIDENCE_TYPE,
        "observation_kind": "SETTLEMENT_RULE",
        "observation": {
            "rule_id": rule.rule_id,
            "rule_version": rule.rule_version,
            "provider_id": rule.scope.provider_id,
            "account_id": rule.scope.account_id,
            "environment": rule.scope.environment,
            "instrument_version": rule.instrument_version,
            "settlement_currency": rule.settlement_currency,
            "effective_from": rule.effective_from.isoformat(),
            "effective_to": (
                None if rule.effective_to is None else rule.effective_to.isoformat()
            ),
            "trade_date": trade_date.isoformat(),
            "expected_settlement_date": expected_settlement_date.isoformat(),
        },
    }


def settlement_rule_evidence_metadata(
    rule: SettlementRuleBinding,
    *,
    trade_date: date,
    expected_settlement_date: date,
) -> dict[str, object]:
    return {
        "evidence_type": SETTLEMENT_EVIDENCE_TYPE,
        "observation_kind": "SETTLEMENT_RULE",
        "rule_id": rule.rule_id,
        "rule_version": rule.rule_version,
        "provider_id": rule.scope.provider_id,
        "account_id": rule.scope.account_id,
        "environment": rule.scope.environment,
        "instrument_version": rule.instrument_version,
        "settlement_currency": rule.settlement_currency,
        "trade_date": trade_date.isoformat(),
        "expected_settlement_date": expected_settlement_date.isoformat(),
    }


def verify_settlement_rule_evidence(
    rule: SettlementRuleBinding,
    artifact_store: ArtifactStore,
    *,
    trade_date: date,
    expected_settlement_date: date,
) -> tuple[str, ...]:
    refs = tuple(
        reference
        for reference in rule.evidence_refs
        if isinstance(reference, str) and reference.startswith("artifact:")
    )
    if not refs:
        raise SettlementConflict(
            "settlement rule requires trusted artifact evidence"
        )
    verified = tuple(
        _verify_artifact(
            artifact_store,
            evidence_ref=reference,
            expected_receipt=settlement_rule_evidence_receipt(
                rule,
                trade_date=trade_date,
                expected_settlement_date=expected_settlement_date,
            ),
            expected_metadata=settlement_rule_evidence_metadata(
                rule,
                trade_date=trade_date,
                expected_settlement_date=expected_settlement_date,
            ),
            name="settlement rule",
        )
        for reference in refs
    )
    return verified


def _rule_from_payload(value: Mapping[str, object]) -> SettlementRuleBinding:
    if not isinstance(value, Mapping):
        raise SettlementConflict("settlement rule payload must be an object")
    raw_scope = value.get("scope")
    if not isinstance(raw_scope, Mapping):
        raise SettlementConflict("settlement rule scope must be an object")
    try:
        effective_from = date.fromisoformat(str(value.get("effective_from")))
        raw_to = value.get("effective_to")
        effective_to = None if raw_to is None else date.fromisoformat(str(raw_to))
    except ValueError as error:
        raise SettlementConflict("settlement rule dates are invalid") from error
    refs = value.get("evidence_refs")
    if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
        raise SettlementConflict("settlement rule evidence_refs are invalid")
    rule = SettlementRuleBinding(
        rule_id=value.get("rule_id"),
        rule_version=value.get("rule_version"),
        scope=SettlementAccountScope(
            provider_id=raw_scope.get("provider_id"),
            account_id=raw_scope.get("account_id"),
            environment=raw_scope.get("environment"),
        ),
        instrument_version=value.get("instrument_version"),
        settlement_currency=value.get("settlement_currency"),
        effective_from=effective_from,
        effective_to=effective_to,
        evidence_refs=tuple(refs),
    )
    if value.get("digest") != rule.digest:
        raise SettlementConflict("settlement rule digest does not match payload")
    return rule


def _obligation_payload(obligation: SettlementObligation) -> dict[str, object]:
    if obligation.source_transaction_id is None or obligation.rule_binding is None:
        raise SettlementConflict(
            "durable settlement obligation requires source transaction and rule binding"
        )
    return {
        "obligation_id": obligation.obligation_id,
        "cause_event_id": obligation.cause_event_id,
        "currency": obligation.currency,
        "amount": _decimal_text(obligation.amount),
        "trade_date": obligation.trade_date.isoformat(),
        "settlement_date": obligation.settlement_date.isoformat(),
        "component_id": obligation.component_id,
        "source_transaction_id": obligation.source_transaction_id,
        "rule_binding": _rule_payload(obligation.rule_binding),
    }


def _obligation_from_payload(value: Mapping[str, object]) -> SettlementObligation:
    if not isinstance(value, Mapping):
        raise SettlementConflict("settlement obligation payload must be an object")
    try:
        trade_date = date.fromisoformat(str(value.get("trade_date")))
        settlement_date = date.fromisoformat(str(value.get("settlement_date")))
    except ValueError as error:
        raise SettlementConflict("settlement obligation dates are invalid") from error
    rule = _rule_from_payload(value.get("rule_binding"))
    return SettlementObligation(
        obligation_id=value.get("obligation_id"),
        cause_event_id=value.get("cause_event_id"),
        currency=value.get("currency"),
        amount=value.get("amount"),
        trade_date=trade_date,
        settlement_date=settlement_date,
        component_id=value.get("component_id"),
        source_transaction_id=value.get("source_transaction_id"),
        rule_binding=rule,
    )


def settlement_completion_evidence_receipt(
    *,
    scope: SettlementAccountScope,
    obligation: SettlementObligation,
    evidence: SettlementEvidence,
) -> dict[str, object]:
    if not isinstance(scope, SettlementAccountScope):
        raise TypeError("scope must be SettlementAccountScope")
    if not isinstance(obligation, SettlementObligation):
        raise TypeError("obligation must be SettlementObligation")
    if not isinstance(evidence, SettlementEvidence):
        raise TypeError("evidence must be SettlementEvidence")
    return {
        "schema_version": SETTLEMENT_EVIDENCE_SCHEMA_VERSION,
        "evidence_type": SETTLEMENT_EVIDENCE_TYPE,
        "observation_kind": "SETTLEMENT_COMPLETION",
        "observation": {
            "provider_id": scope.provider_id,
            "account_id": scope.account_id,
            "environment": scope.environment,
            "obligation_id": obligation.obligation_id,
            "cause_event_id": obligation.cause_event_id,
            "source_transaction_id": obligation.source_transaction_id,
            "currency": obligation.currency,
            "signed_amount": _decimal_text(obligation.amount),
            "trade_date": obligation.trade_date.isoformat(),
            "settlement_date": obligation.settlement_date.isoformat(),
            "component_id": obligation.component_id,
            "observed_at": evidence.observed_at.isoformat().replace("+00:00", "Z"),
        },
    }


def settlement_completion_evidence_metadata(
    *,
    scope: SettlementAccountScope,
    obligation: SettlementObligation,
    evidence: SettlementEvidence,
) -> dict[str, object]:
    return {
        "evidence_type": SETTLEMENT_EVIDENCE_TYPE,
        "observation_kind": "SETTLEMENT_COMPLETION",
        "provider_id": scope.provider_id,
        "account_id": scope.account_id,
        "environment": scope.environment,
        "obligation_id": obligation.obligation_id,
        "cause_event_id": obligation.cause_event_id,
        "source_transaction_id": obligation.source_transaction_id,
        "currency": obligation.currency,
        "signed_amount": _decimal_text(obligation.amount),
        "settlement_date": obligation.settlement_date.isoformat(),
        "observed_at": evidence.observed_at.isoformat().replace("+00:00", "Z"),
    }


def verify_settlement_completion_evidence(
    *,
    scope: SettlementAccountScope,
    obligation: SettlementObligation,
    evidence: SettlementEvidence,
    artifact_store: ArtifactStore,
) -> str:
    if evidence.obligation_id != obligation.obligation_id:
        raise SettlementConflict("settlement evidence identity mismatch")
    return _verify_artifact(
        artifact_store,
        evidence_ref=evidence.evidence_ref,
        expected_receipt=settlement_completion_evidence_receipt(
            scope=scope,
            obligation=obligation,
            evidence=evidence,
        ),
        expected_metadata=settlement_completion_evidence_metadata(
            scope=scope,
            obligation=obligation,
            evidence=evidence,
        ),
        name="settlement completion",
    )


def _evidence_payload(evidence: SettlementEvidence) -> dict[str, str]:
    return {
        "obligation_id": evidence.obligation_id,
        "evidence_ref": evidence.evidence_ref,
        "observed_at": evidence.observed_at.isoformat().replace("+00:00", "Z"),
    }


def _evidence_from_payload(value: Mapping[str, object]) -> SettlementEvidence:
    if not isinstance(value, Mapping):
        raise SettlementConflict("settlement evidence payload must be an object")
    raw_observed = value.get("observed_at")
    if not isinstance(raw_observed, str):
        raise SettlementConflict("settlement evidence observed_at is invalid")
    observed = datetime.fromisoformat(
        _instant(raw_observed, name="observed_at").replace("Z", "+00:00")
    )
    return SettlementEvidence(
        obligation_id=value.get("obligation_id"),
        evidence_ref=value.get("evidence_ref"),
        observed_at=observed,
    )


@dataclass(frozen=True)
class PreparedSettlementMutation:
    envelope: dict[str, object] | None
    request: dict[str, object]
    result: dict[str, object]
    aggregate_version: int
    already_committed: bool = False


class DurableSettlementBook:
    """JournalStore-backed provenance facade for the canonical SettlementBook."""

    def __init__(
        self,
        store: JournalStore,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
        evidence_artifact_store: ArtifactStore,
    ) -> None:
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        self.store = store
        if not isinstance(evidence_artifact_store, ArtifactStore):
            raise TypeError("evidence_artifact_store must be trusted ArtifactStore")
        self.evidence_artifact_store = evidence_artifact_store
        self.scope = SettlementAccountScope(
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
        )
        self.scope_id = _scope_id(self.scope)
        self._book = SettlementBook()
        self._reload()

    def _events(self) -> list[dict[str, object]]:
        return self.store.load_events(_AGGREGATE_TYPE, self.scope_id)

    def _replay(self, events: list[dict[str, object]]) -> SettlementBook:
        book = SettlementBook()
        expected_version = 1
        for event in events:
            if event.get("aggregate_version") != expected_version:
                raise SettlementConflict(
                    "settlement journal aggregate versions are not contiguous"
                )
            expected_version += 1
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise SettlementConflict("settlement event payload must be an object")
            raw_scope = payload.get("scope")
            if raw_scope != _scope_payload(self.scope):
                raise SettlementConflict(
                    "settlement journal event scope does not match durable book"
                )
            event_type = event.get("event_type")
            if event_type == _REGISTER_EVENT:
                raw_items = payload.get("obligations")
                if not isinstance(raw_items, list) or not raw_items:
                    raise SettlementConflict(
                        "settlement registration event requires obligations"
                    )
                if payload.get("batch_digest") != payload_digest(raw_items):
                    raise SettlementConflict(
                        "settlement obligation batch digest does not match payload"
                    )
                for raw in raw_items:
                    obligation = _obligation_from_payload(raw)
                    assert obligation.rule_binding is not None
                    verify_settlement_rule_evidence(
                        obligation.rule_binding,
                        self.evidence_artifact_store,
                        trade_date=obligation.trade_date,
                        expected_settlement_date=obligation.settlement_date,
                    )
                    book.add(obligation)
            elif event_type == _SETTLE_EVENT:
                evidence = _evidence_from_payload(payload.get("evidence"))
                raw_as_of = payload.get("as_of")
                if not isinstance(raw_as_of, str):
                    raise SettlementConflict("settlement event as_of is invalid")
                try:
                    as_of = date.fromisoformat(raw_as_of)
                except ValueError as error:
                    raise SettlementConflict("settlement event as_of is invalid") from error
                obligation = next(
                    (
                        item
                        for item in book.obligations
                        if item.obligation_id == evidence.obligation_id
                    ),
                    None,
                )
                if obligation is None:
                    raise SettlementConflict(
                        "settlement evidence references unknown obligation"
                    )
                verify_settlement_completion_evidence(
                    scope=self.scope,
                    obligation=obligation,
                    evidence=evidence,
                    artifact_store=self.evidence_artifact_store,
                )
                book.settle(
                    evidence.obligation_id,
                    as_of=as_of,
                    settlement_evidence=evidence,
                )
            else:
                raise SettlementConflict(
                    "settlement journal contains an unsupported event type"
                )
        return book

    def _reload(self) -> None:
        self._book = self._replay(self._events())

    def refresh(self) -> None:
        self._reload()

    @property
    def obligations(self) -> tuple[SettlementObligation, ...]:
        return self._book.obligations

    @property
    def settled_obligation_evidence(self) -> dict[str, SettlementEvidence]:
        return self._book.settled_obligation_evidence

    def prepare_register_mutation(
        self,
        obligations: Iterable[SettlementObligation],
        *,
        committed_at: str,
    ) -> PreparedSettlementMutation:
        batch = tuple(obligations)
        if not batch:
            raise ValueError("settlement obligation batch must not be empty")
        canonical = tuple(
            _obligation_from_payload(_obligation_payload(item))
            for item in batch
        )
        for obligation in canonical:
            assert obligation.rule_binding is not None
            if obligation.rule_binding.scope != self.scope:
                raise SettlementConflict(
                    "settlement obligation scope does not match durable book"
                )
            verify_settlement_rule_evidence(
                obligation.rule_binding,
                self.evidence_artifact_store,
                trade_date=obligation.trade_date,
                expected_settlement_date=obligation.settlement_date,
            )

        events = self._events()
        candidate = self._replay(events)
        outcomes = tuple(candidate.add(item) for item in canonical)
        raw_items = [_obligation_payload(item) for item in canonical]
        request = {
            "schema_version": "1.0.0",
            "scope": _scope_payload(self.scope),
            "obligations": raw_items,
        }
        result = {
            "obligation_ids": [item.obligation_id for item in canonical],
            "batch_digest": payload_digest(raw_items),
        }

        if any(outcomes) and not all(outcomes):
            raise SettlementConflict(
                "settlement obligation batch is only partially committed"
            )
        if not any(outcomes):
            matches = [
                event
                for event in events
                if event.get("event_type") == _REGISTER_EVENT
                and isinstance(event.get("payload"), Mapping)
                and event["payload"].get("obligations") == raw_items
            ]
            if len(matches) != 1:
                raise SettlementConflict(
                    "settlement obligations already exist without one canonical batch"
                )
            return PreparedSettlementMutation(
                envelope=None,
                request=request,
                result=result,
                aggregate_version=int(matches[0]["aggregate_version"]),
                already_committed=True,
            )

        next_version = 1 if not events else int(events[-1]["aggregate_version"]) + 1
        when = _instant(committed_at, name="committed_at")
        digest = payload_digest(raw_items)
        payload = {
            "schema_version": "1.0.0",
            "scope": _scope_payload(self.scope),
            "obligations": raw_items,
            "batch_digest": digest,
        }
        envelope = {
            "event_id": _event_id(self.scope, "obligation-batch", digest),
            "event_type": _REGISTER_EVENT,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.scope_id,
            "aggregate_version": str(next_version),
            "committed_at": when,
            "payload": payload,
            "payload_hash": payload_digest(payload),
        }
        return PreparedSettlementMutation(
            envelope=envelope,
            request=request,
            result=result,
            aggregate_version=next_version,
        )

    def register_obligations(
        self,
        obligations: Iterable[SettlementObligation],
        *,
        command_id: str,
        idempotency_key: str,
        committed_at: str,
    ) -> bool:
        plan = self.prepare_register_mutation(
            obligations,
            committed_at=committed_at,
        )
        if plan.already_committed:
            self._reload()
            return False
        assert plan.envelope is not None
        try:
            _, inserted, _ = self.store.commit_command(
                command_id=_text(command_id, name="command_id"),
                actor=_ACTOR,
                environment=self.scope.environment,
                idempotency_key=_text(
                    idempotency_key, name="idempotency_key"
                ),
                request=plan.request,
                result=plan.result,
                state_version=plan.aggregate_version,
                events=[(plan.envelope, None)],
            )
        except Exception:
            self._reload()
            raise
        self._reload()
        return inserted

    def prepare_settlement_mutation(
        self,
        evidence: SettlementEvidence,
        *,
        as_of: date,
        committed_at: str,
    ) -> PreparedSettlementMutation:
        if not isinstance(evidence, SettlementEvidence):
            raise TypeError("evidence must be SettlementEvidence")
        if type(as_of) is not date:
            raise TypeError("as_of must be a date value")
        events = self._events()
        candidate = self._replay(events)
        obligation = next(
            (
                item
                for item in candidate.obligations
                if item.obligation_id == evidence.obligation_id
            ),
            None,
        )
        if obligation is None:
            raise SettlementConflict("unknown settlement obligation")
        verify_settlement_completion_evidence(
            scope=self.scope,
            obligation=obligation,
            evidence=evidence,
            artifact_store=self.evidence_artifact_store,
        )
        inserted = candidate.settle(
            evidence.obligation_id,
            as_of=as_of,
            settlement_evidence=evidence,
        )
        request = {
            "schema_version": "1.0.0",
            "scope": _scope_payload(self.scope),
            "evidence": _evidence_payload(evidence),
            "as_of": as_of.isoformat(),
        }
        result = {
            "obligation_id": evidence.obligation_id,
            "evidence_ref": evidence.evidence_ref,
        }
        if not inserted:
            matches = [
                event
                for event in events
                if event.get("event_type") == _SETTLE_EVENT
                and isinstance(event.get("payload"), Mapping)
                and event["payload"].get("evidence") == _evidence_payload(evidence)
            ]
            if len(matches) != 1:
                raise SettlementConflict(
                    "settlement evidence exists without one canonical durable event"
                )
            return PreparedSettlementMutation(
                envelope=None,
                request=request,
                result=result,
                aggregate_version=int(matches[0]["aggregate_version"]),
                already_committed=True,
            )

        next_version = 1 if not events else int(events[-1]["aggregate_version"]) + 1
        when = _instant(committed_at, name="committed_at")
        evidence_value = _evidence_payload(evidence)
        payload = {
            "schema_version": "1.0.0",
            "scope": _scope_payload(self.scope),
            "evidence": evidence_value,
            "as_of": as_of.isoformat(),
        }
        envelope = {
            "event_id": _event_id(
                self.scope,
                "settlement-evidence",
                evidence_value,
            ),
            "event_type": _SETTLE_EVENT,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.scope_id,
            "aggregate_version": str(next_version),
            "committed_at": when,
            "payload": payload,
            "payload_hash": payload_digest(payload),
        }
        return PreparedSettlementMutation(
            envelope=envelope,
            request=request,
            result=result,
            aggregate_version=next_version,
        )

    def apply_settlement(
        self,
        evidence: SettlementEvidence,
        *,
        as_of: date,
        command_id: str,
        idempotency_key: str,
        committed_at: str,
    ) -> bool:
        plan = self.prepare_settlement_mutation(
            evidence,
            as_of=as_of,
            committed_at=committed_at,
        )
        if plan.already_committed:
            self._reload()
            return False
        assert plan.envelope is not None
        try:
            _, inserted, _ = self.store.commit_command(
                command_id=_text(command_id, name="command_id"),
                actor=_ACTOR,
                environment=self.scope.environment,
                idempotency_key=_text(
                    idempotency_key, name="idempotency_key"
                ),
                request=plan.request,
                result=plan.result,
                state_version=plan.aggregate_version,
                events=[(plan.envelope, None)],
            )
        except Exception:
            self._reload()
            raise
        self._reload()
        return inserted

    def project(self, economic_book) -> SettlementBook:
        """Rebuild cash availability from canonical economics plus durable provenance."""

        return SettlementBook.from_economic_book(
            economic_book=economic_book,
            obligations=self.obligations,
            settled_obligation_evidence=self.settled_obligation_evidence,
        )
