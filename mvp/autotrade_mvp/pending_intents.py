"""Durable server-owned pending financial intent authority.

This is a bounded prerequisite for product-owned CONFIRM_INTENT.  It does not
perform risk admission, issue trading authority, or send provider requests.
The registry owns one immutable RiskIntent per pending identifier in the same
canonical JournalStore used by the financial authority lineage.  A confirmation
path supplies only an opaque pending id plus authenticated scope; the financial
fields are reconstructed from durable server state rather than from the client.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import UUID

from .exact_decimal import canonical_decimal_text
from .persistence import JournalStore, payload_digest
from .risk import RiskIntent


class PendingIntentError(RuntimeError):
    """Raised when pending financial intent authority is unavailable or invalid."""


_AGGREGATE_TYPE = "pending_financial_intent"
_REGISTERED_EVENT = "PendingFinancialIntentRegistered"
_CONFIRMATION_CLAIMED_EVENT = "PendingFinancialIntentConfirmationClaimed"
_PENDING_FACTORY = object()
_EVENT_KEYS = frozenset(
    {
        "event_id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "payload",
        "payload_hash",
        "committed_at",
        "journal_sequence",
    }
)
_REGISTERED_KEYS = frozenset(
    {
        "pending_intent_id",
        "account_id",
        "environment",
        "policy_id",
        "authority_policy_version",
        "instrument_id",
        "instrument_version",
        "risk_intent",
        "registered_at",
        "expires_at",
        "intent_hash",
    }
)
_CLAIM_KEYS = frozenset(
    {
        "pending_intent_id",
        "registered_event_id",
        "intent_hash",
        "confirmation_id",
        "actor_id",
        "claimed_at",
    }
)

# Retain installed durable primitives once.  Public class/module rebinding must
# not redirect server-owned intent publication or readback after import.
_CANONICAL_JOURNAL_STORE_IDENTITY = JournalStore.store_identity
_CANONICAL_JOURNAL_APPEND_EVENT = JournalStore.append_event
_CANONICAL_JOURNAL_LOAD_EVENTS = JournalStore.load_events
_CANONICAL_PAYLOAD_DIGEST = payload_digest


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PendingIntentError(f"{name} must be exact canonical non-empty text")
    return value


def _environment(value: object) -> str:
    environment = _text(value, name="environment").upper()
    if environment not in {"SIMULATION", "PAPER", "LIVE"}:
        raise PendingIntentError(
            "pending financial intent requires SIMULATION, PAPER or LIVE environment"
        )
    return environment


def _instrument_id(value: object) -> str:
    raw = _text(value, name="instrument_id")
    try:
        canonical = str(UUID(raw))
    except (TypeError, ValueError, AttributeError) as error:
        raise PendingIntentError("instrument_id must be a canonical UUID") from error
    if raw.lower() != canonical:
        raise PendingIntentError("instrument_id must be a canonical UUID")
    return canonical


def _utc(value: object, *, name: str) -> datetime:
    # Reject nested caller code before any tzinfo callback can execute.
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise PendingIntentError(
            f"{name} must use an exact datetime with a built-in timezone"
        )
    if datetime.utcoffset(value) is None:
        raise PendingIntentError(f"{name} must be timezone-aware")
    return datetime.astimezone(value, timezone.utc)


def _utc_text(value: object, *, name: str) -> str:
    return _utc(value, name=name).isoformat().replace("+00:00", "Z")


def _parse_utc_text(value: object, *, name: str) -> datetime:
    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise PendingIntentError(f"{name} must be canonical UTC text")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise PendingIntentError(f"{name} must be canonical UTC text") from error
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise PendingIntentError(f"{name} must be canonical UTC text")
    return parsed


def _risk_intent_payload(intent: object) -> dict[str, object]:
    if type(intent) is not RiskIntent:
        raise PendingIntentError("risk_intent must be exact RiskIntent")
    # Re-admit every field at the trust boundary.  A frozen dataclass can still
    # be mutated with object.__setattr__, so type identity alone is insufficient.
    try:
        admitted = RiskIntent.create(
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            price=intent.price,
            expected_state_version=intent.expected_state_version,
            reduce_only=intent.reduce_only,
            action=intent.action,
            instrument_type=intent.instrument_type,
        )
    except (TypeError, ValueError) as error:
        raise PendingIntentError("risk_intent is no longer canonical") from error
    if admitted != intent:
        raise PendingIntentError("risk_intent is no longer canonical")
    return {
        "symbol": admitted.symbol,
        "side": admitted.side,
        "quantity": canonical_decimal_text(admitted.quantity),
        "price": canonical_decimal_text(admitted.price),
        "expected_state_version": admitted.expected_state_version,
        "reduce_only": admitted.reduce_only,
        "action": admitted.action,
        "instrument_type": admitted.instrument_type,
    }


def _risk_intent_from_payload(value: object) -> RiskIntent:
    if type(value) is not dict or set(value) != {
        "symbol",
        "side",
        "quantity",
        "price",
        "expected_state_version",
        "reduce_only",
        "action",
        "instrument_type",
    }:
        raise PendingIntentError("durable pending risk_intent schema is invalid")
    try:
        return RiskIntent.create(
            symbol=value["symbol"],
            side=value["side"],
            quantity=value["quantity"],
            price=value["price"],
            expected_state_version=value["expected_state_version"],
            reduce_only=value["reduce_only"],
            action=value["action"],
            instrument_type=value["instrument_type"],
        )
    except (TypeError, ValueError) as error:
        raise PendingIntentError("durable pending risk_intent is invalid") from error


def _intent_hash(
    *,
    account_id: str,
    environment: str,
    policy_id: str,
    authority_policy_version: int,
    instrument_id: str,
    instrument_version: int,
    risk_intent_payload: dict[str, object],
) -> str:
    material = {
        "schema_version": 1,
        "account_id": account_id,
        "environment": environment,
        "policy_id": policy_id,
        "authority_policy_version": authority_policy_version,
        "instrument_id": instrument_id,
        "instrument_version": instrument_version,
        "risk_intent": risk_intent_payload,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PendingFinancialIntent:
    """Immutable value issued only from the durable pending-intent registry."""

    pending_intent_id: str
    account_id: str
    environment: str
    policy_id: str
    authority_policy_version: int
    instrument_id: str
    instrument_version: int
    risk_intent: RiskIntent
    registered_at: str
    expires_at: str
    intent_hash: str
    _factory: InitVar[object | None] = None

    def __post_init__(self, _factory: object | None) -> None:
        if _factory is not _PENDING_FACTORY:
            raise PendingIntentError(
                "pending financial intent must come from durable server registry"
            )
        pending_id = _text(self.pending_intent_id, name="pending_intent_id")
        account = _text(self.account_id, name="account_id")
        environment = _environment(self.environment)
        policy = _text(self.policy_id, name="policy_id")
        version = self.authority_policy_version
        if type(version) is not int or version < 1:
            raise PendingIntentError("authority_policy_version must be positive")
        instrument_id = _instrument_id(self.instrument_id)
        instrument_version = self.instrument_version
        if type(instrument_version) is not int or instrument_version < 1:
            raise PendingIntentError("instrument_version must be positive")
        risk_payload = _risk_intent_payload(self.risk_intent)
        registered_at = _text(self.registered_at, name="registered_at")
        expires_at = _text(self.expires_at, name="expires_at")
        registered = _parse_utc_text(registered_at, name="registered_at")
        expires = _parse_utc_text(expires_at, name="expires_at")
        if registered >= expires:
            raise PendingIntentError("pending intent must expire after registration")
        expected_hash = _intent_hash(
            account_id=account,
            environment=environment,
            policy_id=policy,
            authority_policy_version=version,
            instrument_id=instrument_id,
            instrument_version=instrument_version,
            risk_intent_payload=risk_payload,
        )
        if self.intent_hash != expected_hash:
            raise PendingIntentError("pending intent hash does not match exact economics")
        object.__setattr__(self, "pending_intent_id", pending_id)
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "policy_id", policy)
        object.__setattr__(self, "instrument_id", instrument_id)


def _registered_payload(value: PendingFinancialIntent) -> dict[str, object]:
    if type(value) is not PendingFinancialIntent:
        raise PendingIntentError("exact PendingFinancialIntent is required")
    # __post_init__ already admitted this instance; re-admit nested economics at
    # persistence boundaries so post-construction mutation still fails closed.
    risk_payload = _risk_intent_payload(value.risk_intent)
    return {
        "pending_intent_id": value.pending_intent_id,
        "account_id": value.account_id,
        "environment": value.environment,
        "policy_id": value.policy_id,
        "authority_policy_version": value.authority_policy_version,
        "instrument_id": value.instrument_id,
        "instrument_version": value.instrument_version,
        "risk_intent": risk_payload,
        "registered_at": value.registered_at,
        "expires_at": value.expires_at,
        "intent_hash": value.intent_hash,
    }


def _event(
    *,
    event_id: str,
    event_type: str,
    aggregate_id: str,
    version: int,
    payload: dict[str, object],
    committed_at: str,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": _AGGREGATE_TYPE,
        "aggregate_id": aggregate_id,
        "aggregate_version": str(version),
        "payload": payload,
        "payload_hash": _CANONICAL_PAYLOAD_DIGEST(payload),
        "committed_at": committed_at,
    }


def _require_event(
    event: object,
    *,
    pending_intent_id: str,
    event_type: str,
    version: int,
    payload_keys: frozenset[str],
) -> dict[str, object]:
    if type(event) is not dict or set(event) != _EVENT_KEYS:
        raise PendingIntentError("pending intent journal event schema is invalid")
    suffix = "registered" if event_type == _REGISTERED_EVENT else "confirmation"
    if (
        event.get("event_id") != f"pending-intent:{pending_intent_id}:{suffix}"
        or event.get("event_type") != event_type
        or event.get("aggregate_type") != _AGGREGATE_TYPE
        or event.get("aggregate_id") != pending_intent_id
        or event.get("aggregate_version") != version
    ):
        raise PendingIntentError("pending intent journal chronology is invalid")
    payload = event.get("payload")
    if type(payload) is not dict or set(payload) != payload_keys:
        raise PendingIntentError("pending intent durable payload schema is invalid")
    if event.get("payload_hash") != _CANONICAL_PAYLOAD_DIGEST(payload):
        raise PendingIntentError("pending intent durable payload hash mismatch")
    committed_at = _text(event.get("committed_at"), name="committed_at")
    _parse_utc_text(committed_at, name="committed_at")
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence < 1:
        raise PendingIntentError("pending intent journal sequence is invalid")
    return payload


class DurablePendingIntentRegistry:
    """Journal-backed server-owned pending financial intent registry."""

    def __init__(self, store: JournalStore) -> None:
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")
        self._store = store
        self._store_identity = _CANONICAL_JOURNAL_STORE_IDENTITY.__get__(
            store, JournalStore
        )

    @property
    def store(self) -> JournalStore:
        return self._require_store()

    def _require_store(self) -> JournalStore:
        if type(self._store) is not JournalStore:
            raise PendingIntentError("pending intent JournalStore authority changed")
        identity = _CANONICAL_JOURNAL_STORE_IDENTITY.__get__(
            self._store, JournalStore
        )
        if identity != self._store_identity:
            raise PendingIntentError("pending intent JournalStore generation changed")
        return self._store

    def _events(self, pending_intent_id: str) -> list[dict[str, object]]:
        pending_id = _text(pending_intent_id, name="pending_intent_id")
        return _CANONICAL_JOURNAL_LOAD_EVENTS(
            self._require_store(),
            _AGGREGATE_TYPE,
            pending_id,
        )

    def _read(
        self, pending_intent_id: str
    ) -> tuple[PendingFinancialIntent, dict[str, object] | None]:
        pending_id = _text(pending_intent_id, name="pending_intent_id")
        events = self._events(pending_id)
        if len(events) not in {1, 2}:
            raise PendingIntentError(
                "pending financial intent is missing or journal chronology is ambiguous"
            )
        registered_payload = _require_event(
            events[0],
            pending_intent_id=pending_id,
            event_type=_REGISTERED_EVENT,
            version=1,
            payload_keys=_REGISTERED_KEYS,
        )
        if registered_payload.get("pending_intent_id") != pending_id:
            raise PendingIntentError("pending intent durable id binding is invalid")
        risk_intent = _risk_intent_from_payload(registered_payload.get("risk_intent"))
        value = PendingFinancialIntent(
            pending_intent_id=pending_id,
            account_id=registered_payload.get("account_id"),
            environment=registered_payload.get("environment"),
            policy_id=registered_payload.get("policy_id"),
            authority_policy_version=registered_payload.get("authority_policy_version"),
            instrument_id=registered_payload.get("instrument_id"),
            instrument_version=registered_payload.get("instrument_version"),
            risk_intent=risk_intent,
            registered_at=registered_payload.get("registered_at"),
            expires_at=registered_payload.get("expires_at"),
            intent_hash=registered_payload.get("intent_hash"),
            _factory=_PENDING_FACTORY,
        )
        if _registered_payload(value) != registered_payload:
            raise PendingIntentError("pending intent durable payload is non-canonical")
        claim = None
        if len(events) == 2:
            claim = _require_event(
                events[1],
                pending_intent_id=pending_id,
                event_type=_CONFIRMATION_CLAIMED_EVENT,
                version=2,
                payload_keys=_CLAIM_KEYS,
            )
            if (
                claim.get("pending_intent_id") != pending_id
                or claim.get("registered_event_id") != events[0].get("event_id")
                or claim.get("intent_hash") != value.intent_hash
            ):
                raise PendingIntentError(
                    "pending confirmation claim lost exact registered intent binding"
                )
        return value, claim

    def register(
        self,
        *,
        pending_intent_id: str,
        account_id: str,
        environment: str,
        policy_id: str,
        authority_policy_version: int,
        instrument_id: str,
        instrument_version: int,
        risk_intent: RiskIntent,
        registered_at: datetime,
        expires_at: datetime,
    ) -> PendingFinancialIntent:
        pending_id = _text(pending_intent_id, name="pending_intent_id")
        account = _text(account_id, name="account_id")
        env = _environment(environment)
        policy = _text(policy_id, name="policy_id")
        if type(authority_policy_version) is not int or authority_policy_version < 1:
            raise PendingIntentError("authority_policy_version must be positive")
        canonical_instrument_id = _instrument_id(instrument_id)
        if type(instrument_version) is not int or instrument_version < 1:
            raise PendingIntentError("instrument_version must be positive")
        risk_payload = _risk_intent_payload(risk_intent)
        registered_text = _utc_text(registered_at, name="registered_at")
        expires_text = _utc_text(expires_at, name="expires_at")
        if _parse_utc_text(registered_text, name="registered_at") >= _parse_utc_text(
            expires_text, name="expires_at"
        ):
            raise PendingIntentError("pending intent must expire after registration")
        digest = _intent_hash(
            account_id=account,
            environment=env,
            policy_id=policy,
            authority_policy_version=authority_policy_version,
            instrument_id=canonical_instrument_id,
            instrument_version=instrument_version,
            risk_intent_payload=risk_payload,
        )
        value = PendingFinancialIntent(
            pending_intent_id=pending_id,
            account_id=account,
            environment=env,
            policy_id=policy,
            authority_policy_version=authority_policy_version,
            instrument_id=canonical_instrument_id,
            instrument_version=instrument_version,
            risk_intent=risk_intent,
            registered_at=registered_text,
            expires_at=expires_text,
            intent_hash=digest,
            _factory=_PENDING_FACTORY,
        )
        payload = _registered_payload(value)
        try:
            _CANONICAL_JOURNAL_APPEND_EVENT(
                self._require_store(),
                _event(
                    event_id=f"pending-intent:{pending_id}:registered",
                    event_type=_REGISTERED_EVENT,
                    aggregate_id=pending_id,
                    version=1,
                    payload=payload,
                    committed_at=registered_text,
                ),
            )
        except ValueError as error:
            try:
                existing, claim = self._read(pending_id)
            except PendingIntentError:
                raise PendingIntentError(
                    "pending intent id is already used by conflicting durable state"
                ) from error
            if claim is not None or _registered_payload(existing) != payload:
                raise PendingIntentError(
                    "pending intent id is already used by conflicting durable state"
                ) from error
            return existing
        return self.resolve(
            pending_id,
            account_id=account,
            environment=env,
            policy_id=policy,
            authority_policy_version=authority_policy_version,
            at=registered_at,
        )

    def resolve(
        self,
        pending_intent_id: str,
        *,
        account_id: str,
        environment: str,
        policy_id: str,
        authority_policy_version: int,
        at: datetime,
    ) -> PendingFinancialIntent:
        value, _claim = self._read(pending_intent_id)
        expected = (
            _text(account_id, name="account_id"),
            _environment(environment),
            _text(policy_id, name="policy_id"),
            authority_policy_version,
        )
        if type(authority_policy_version) is not int or authority_policy_version < 1:
            raise PendingIntentError("authority_policy_version must be positive")
        actual = (
            value.account_id,
            value.environment,
            value.policy_id,
            value.authority_policy_version,
        )
        if actual != expected:
            raise PendingIntentError(
                "pending intent scope differs from authenticated confirmation scope"
            )
        point = _utc(at, name="at")
        registered = _parse_utc_text(value.registered_at, name="registered_at")
        expires = _parse_utc_text(value.expires_at, name="expires_at")
        if not registered <= point < expires:
            raise PendingIntentError("pending intent is not current at confirmation time")
        return value

    def claim_confirmation(
        self,
        pending_intent_id: str,
        *,
        confirmation_id: str,
        actor_id: str,
        account_id: str,
        environment: str,
        policy_id: str,
        authority_policy_version: int,
        at: datetime,
    ) -> PendingFinancialIntent:
        pending_id = _text(pending_intent_id, name="pending_intent_id")
        confirmation = _text(confirmation_id, name="confirmation_id")
        actor = _text(actor_id, name="actor_id")
        value = self.resolve(
            pending_id,
            account_id=account_id,
            environment=environment,
            policy_id=policy_id,
            authority_policy_version=authority_policy_version,
            at=at,
        )
        existing_value, existing_claim = self._read(pending_id)
        if existing_value != value:
            raise PendingIntentError("pending intent changed during confirmation claim")
        claimed_at = _utc_text(at, name="at")
        claim = {
            "pending_intent_id": pending_id,
            "registered_event_id": f"pending-intent:{pending_id}:registered",
            "intent_hash": value.intent_hash,
            "confirmation_id": confirmation,
            "actor_id": actor,
            "claimed_at": claimed_at,
        }
        if existing_claim is not None:
            if existing_claim != claim:
                raise PendingIntentError(
                    "pending intent was already claimed by another confirmation"
                )
            return value
        try:
            _CANONICAL_JOURNAL_APPEND_EVENT(
                self._require_store(),
                _event(
                    event_id=f"pending-intent:{pending_id}:confirmation",
                    event_type=_CONFIRMATION_CLAIMED_EVENT,
                    aggregate_id=pending_id,
                    version=2,
                    payload=claim,
                    committed_at=claimed_at,
                ),
            )
        except ValueError as error:
            try:
                concurrent_value, concurrent_claim = self._read(pending_id)
            except PendingIntentError:
                raise PendingIntentError(
                    "pending confirmation claim conflicted with durable state"
                ) from error
            if concurrent_value != value or concurrent_claim != claim:
                raise PendingIntentError(
                    "pending intent was concurrently claimed by another confirmation"
                ) from error
        final_value, final_claim = self._read(pending_id)
        if final_value != value or final_claim != claim:
            raise PendingIntentError("pending confirmation claim is not durable")
        return final_value
