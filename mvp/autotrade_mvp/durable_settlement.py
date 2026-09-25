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
from uuid import NAMESPACE_URL, uuid5

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
    ) -> None:
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        self.store = store
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
                    book.add(_obligation_from_payload(raw))
            elif event_type == _SETTLE_EVENT:
                evidence = _evidence_from_payload(payload.get("evidence"))
                raw_as_of = payload.get("as_of")
                if not isinstance(raw_as_of, str):
                    raise SettlementConflict("settlement event as_of is invalid")
                try:
                    as_of = date.fromisoformat(raw_as_of)
                except ValueError as error:
                    raise SettlementConflict("settlement event as_of is invalid") from error
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
