"""Deterministic authority and confirmation gates for AutoTrade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Callable, FrozenSet
from uuid import UUID


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, order=True)
class InstrumentVersionIdentity:
    """Immutable authority reference to one canonical instrument version."""

    instrument_id: str
    version: int

    def __post_init__(self) -> None:
        raw_id = _text(self.instrument_id, name="instrument_id")
        try:
            canonical_id = str(UUID(raw_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("instrument_id must be a UUID") from error
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version < 1
        ):
            raise ValueError("instrument_version must be a positive integer")
        object.__setattr__(self, "instrument_id", canonical_id)


def _instrument_identity(value, *, name: str = "instrument") -> InstrumentVersionIdentity:
    if isinstance(value, InstrumentVersionIdentity):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return InstrumentVersionIdentity(value[0], value[1])
    raise TypeError(
        f"{name} must be InstrumentVersionIdentity or an (instrument_id, version) pair"
    )


@dataclass(frozen=True)
class AuthorityPolicy:
    policy_id: str
    account_id: str
    environments: FrozenSet[str]
    instruments: FrozenSet[InstrumentVersionIdentity]
    actions: FrozenSet[str]
    max_notional: Decimal
    expires_at: str
    autonomous: bool
    valid_from: str = "1970-01-01T00:00:00Z"
    protection_only: bool = False

    def __post_init__(self) -> None:
        # The policy object itself is an authority boundary.  Callers can
        # instantiate dataclasses directly, so validation cannot live only in
        # create(); otherwise truthy non-booleans such as "false" could bypass
        # the confirmation requirement through policy.autonomous.
        if not isinstance(self.autonomous, bool) or not isinstance(
            self.protection_only, bool
        ):
            raise TypeError("autonomous and protection_only must be booleans")
        if isinstance(self.environments, (str, bytes)):
            raise TypeError("environments must be a collection")
        if isinstance(self.instruments, (str, bytes)):
            raise TypeError("instruments must be a collection")
        if isinstance(self.actions, (str, bytes)):
            raise TypeError("actions must be a collection")

        normalized_environments = frozenset(
            _text(item, name="environment").upper() for item in self.environments
        )
        if (
            not normalized_environments
            or not normalized_environments <= {"SIMULATION", "PAPER", "LIVE"}
        ):
            raise ValueError("environments must contain supported values")
        normalized_instruments = frozenset(
            _instrument_identity(item) for item in self.instruments
        )
        normalized_actions = frozenset(
            _text(item, name="action").upper() for item in self.actions
        )
        if not normalized_instruments or not normalized_actions:
            raise ValueError("instruments and actions must be non-empty")

        notional = _decimal(self.max_notional, name="max_notional")
        if notional <= 0:
            raise ValueError("max_notional must be positive")
        valid_from = _text(self.valid_from, name="valid_from")
        expires_at = _text(self.expires_at, name="expires_at")
        if _instant(valid_from, name="valid_from") >= _instant(
            expires_at, name="expires_at"
        ):
            raise ValueError("valid_from must precede expires_at")

        object.__setattr__(self, "policy_id", _text(self.policy_id, name="policy_id"))
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(self, "environments", normalized_environments)
        object.__setattr__(self, "instruments", normalized_instruments)
        object.__setattr__(self, "actions", normalized_actions)
        object.__setattr__(self, "max_notional", notional)
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "expires_at", expires_at)

    @classmethod
    def create(
        cls,
        *,
        policy_id: str,
        account_id: str,
        environments,
        instruments,
        actions,
        max_notional,
        expires_at: str,
        autonomous: bool,
        valid_from: str = "1970-01-01T00:00:00Z",
        protection_only: bool = False,
    ) -> "AuthorityPolicy":
        normalized_environments = frozenset(
            _text(item, name="environment").upper() for item in environments
        )
        if not normalized_environments or not normalized_environments <= {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("environments must contain supported values")
        normalized_instruments = frozenset(
            _instrument_identity(item) for item in instruments
        )
        normalized_actions = frozenset(
            _text(item, name="action").upper() for item in actions
        )
        if not normalized_instruments or not normalized_actions:
            raise ValueError("instruments and actions must be non-empty")
        notional = _decimal(max_notional, name="max_notional")
        if notional <= 0:
            raise ValueError("max_notional must be positive")
        valid_from_instant = _instant(valid_from, name="valid_from")
        expires_at_instant = _instant(expires_at, name="expires_at")
        if valid_from_instant >= expires_at_instant:
            raise ValueError("valid_from must precede expires_at")
        if not isinstance(autonomous, bool) or not isinstance(protection_only, bool):
            raise TypeError("autonomous and protection_only must be booleans")
        return cls(
            policy_id=_text(policy_id, name="policy_id"),
            account_id=_text(account_id, name="account_id"),
            environments=normalized_environments,
            instruments=normalized_instruments,
            actions=normalized_actions,
            max_notional=notional,
            expires_at=expires_at,
            autonomous=autonomous,
            valid_from=valid_from,
            protection_only=protection_only,
        )


@dataclass(frozen=True)
class Confirmation:
    confirmation_id: str
    policy_id: str
    intent_hash: str
    account_id: str
    environment: str
    instrument_version: InstrumentVersionIdentity
    action: str
    notional: Decimal
    expires_at: str


@dataclass(frozen=True)
class AdmissionRecord:
    admission_id: str
    policy_id: str
    intent_hash: str
    account_id: str
    environment: str
    instrument_version: InstrumentVersionIdentity
    action: str
    notional: Decimal
    risk_reducing: bool
    state_version: int
    authority_epoch: int
    outcome: str
    admitted_at: str
    confirmation_id: str | None
    reason: str
    request_fingerprint: str


class AuthorityConflict(ValueError):
    """Raised when immutable authority identity is reused inconsistently."""


class AuthorityService:
    def __init__(self):
        self._policies: dict[str, AuthorityPolicy] = {}
        self._revocations: dict[str, tuple[str, str]] = {}
        self._confirmations: dict[str, Confirmation] = {}
        self._used_confirmations: set[str] = set()
        self._admissions: dict[str, AdmissionRecord] = {}
        self._epoch = 0

    @property
    def epoch(self) -> int:
        return self._epoch

    def register_policy(self, policy: AuthorityPolicy) -> bool:
        if not isinstance(policy, AuthorityPolicy):
            raise TypeError("policy must be AuthorityPolicy")
        existing = self._policies.get(policy.policy_id)
        if existing is not None:
            if existing != policy:
                raise AuthorityConflict("policy_id already has different content")
            return False
        self._policies[policy.policy_id] = policy
        self._epoch += 1
        return True

    def revoke_policy(self, policy_id: str, *, reason: str, revoked_at: str) -> bool:
        pid = _text(policy_id, name="policy_id")
        if pid not in self._policies:
            raise KeyError(pid)
        normalized = (_text(reason, name="reason"), revoked_at)
        _instant(revoked_at, name="revoked_at")
        existing = self._revocations.get(pid)
        if existing is not None:
            if existing != normalized:
                raise AuthorityConflict("policy revocation already recorded differently")
            return False
        self._revocations[pid] = normalized
        self._epoch += 1
        return True

    def add_confirmation(
        self,
        *,
        confirmation_id: str,
        policy_id: str,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        notional,
        expires_at: str,
    ) -> bool:
        cid = _text(confirmation_id, name="confirmation_id")
        pid = _text(policy_id, name="policy_id")
        if pid not in self._policies:
            raise KeyError(pid)
        confirmation_notional = _decimal(notional, name="notional")
        if confirmation_notional < 0:
            raise ValueError("notional must be non-negative")
        confirmation = Confirmation(
            confirmation_id=cid,
            policy_id=pid,
            intent_hash=_text(intent_hash, name="intent_hash"),
            account_id=_text(account_id, name="account_id"),
            environment=_text(environment, name="environment").upper(),
            instrument_version=InstrumentVersionIdentity(
                instrument_id, instrument_version
            ),
            action=_text(action, name="action").upper(),
            notional=confirmation_notional,
            expires_at=expires_at,
        )
        _instant(expires_at, name="expires_at")
        existing = self._confirmations.get(cid)
        if existing is not None:
            if existing != confirmation:
                raise AuthorityConflict("confirmation_id already has different content")
            return False
        self._confirmations[cid] = confirmation
        return True

    def _policy_active(self, policy: AuthorityPolicy, now: str) -> tuple[bool, str]:
        current = _instant(now, name="now")
        if current < _instant(policy.valid_from, name="policy.valid_from"):
            return False, "policy_not_yet_active"
        revocation = self._revocations.get(policy.policy_id)
        if revocation is not None:
            _, revoked_at = revocation
            if current >= _instant(revoked_at, name="revoked_at"):
                return False, "policy_revoked"
        if current >= _instant(policy.expires_at, name="policy.expires_at"):
            return False, "policy_expired"
        return True, "active"

    def admit(
        self,
        *,
        admission_id: str,
        policy_id: str,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        notional,
        state_version: int,
        risk_admitted: bool,
        now: str,
        confirmation_id: str | None = None,
        risk_reducing: bool = False,
    ) -> AdmissionRecord:
        aid = _text(admission_id, name="admission_id")
        pid = _text(policy_id, name="policy_id")
        ihash = _text(intent_hash, name="intent_hash")
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        identity = InstrumentVersionIdentity(instrument_id, instrument_version)
        normalized_action = _text(action, name="action").upper()
        if not isinstance(state_version, int) or isinstance(state_version, bool) or state_version < 0:
            raise ValueError("state_version must be a non-negative integer")
        if not isinstance(risk_admitted, bool) or not isinstance(risk_reducing, bool):
            raise TypeError("risk_admitted and risk_reducing must be booleans")
        policy = self._policies.get(pid)
        if policy is None:
            raise KeyError(pid)
        amount = _decimal(notional, name="notional")
        if amount < 0:
            raise ValueError("notional must be non-negative")
        request_payload = {
            "policy_id": pid,
            "intent_hash": ihash,
            "account_id": account,
            "environment": env,
            "instrument_id": identity.instrument_id,
            "instrument_version": identity.version,
            "action": normalized_action,
            "notional": str(amount),
            "state_version": state_version,
            "risk_admitted": risk_admitted,
            "confirmation_id": confirmation_id,
            "risk_reducing": risk_reducing,
        }
        request_fingerprint = sha256(
            json.dumps(request_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        existing_admission = self._admissions.get(aid)
        if existing_admission is not None:
            if existing_admission.request_fingerprint != request_fingerprint:
                raise AuthorityConflict("admission_id already has different request content")
            return existing_admission

        active, reason = self._policy_active(policy, now)
        checks = [
            (active, reason),
            (risk_admitted, "risk_rejected"),
            (account == policy.account_id, "account_out_of_scope"),
            (env in policy.environments, "environment_out_of_scope"),
            (identity in policy.instruments, "instrument_version_out_of_scope"),
            (normalized_action in policy.actions, "action_out_of_scope"),
            (amount <= policy.max_notional, "notional_out_of_scope"),
            (not policy.protection_only or risk_reducing, "protection_policy_requires_risk_reduction"),
        ]
        outcome = "ADMITTED"
        failure_reason = "admitted"
        for passed, failed_reason in checks:
            if not passed:
                outcome = "REJECTED"
                failure_reason = failed_reason
                break

        used_confirmation: str | None = None
        if outcome == "ADMITTED" and not policy.autonomous:
            if confirmation_id is None:
                outcome, failure_reason = "REJECTED", "confirmation_required"
            else:
                confirmation = self._confirmations.get(confirmation_id)
                if confirmation is None:
                    outcome, failure_reason = "REJECTED", "confirmation_missing"
                elif confirmation.confirmation_id in self._used_confirmations:
                    outcome, failure_reason = "REJECTED", "confirmation_already_used"
                elif confirmation.policy_id != pid:
                    outcome, failure_reason = "REJECTED", "confirmation_policy_mismatch"
                elif confirmation.intent_hash != ihash:
                    outcome, failure_reason = "REJECTED", "confirmation_intent_mismatch"
                elif (
                    confirmation.account_id != account
                    or confirmation.environment != env
                    or confirmation.instrument_version != identity
                    or confirmation.action != normalized_action
                    or confirmation.notional != amount
                ):
                    outcome, failure_reason = "REJECTED", "confirmation_scope_mismatch"
                elif _instant(now, name="now") >= _instant(confirmation.expires_at, name="confirmation.expires_at"):
                    outcome, failure_reason = "REJECTED", "confirmation_expired"
                else:
                    used_confirmation = confirmation.confirmation_id

        record = AdmissionRecord(
            admission_id=aid,
            policy_id=pid,
            intent_hash=ihash,
            account_id=account,
            environment=env,
            instrument_version=identity,
            action=normalized_action,
            notional=amount,
            risk_reducing=risk_reducing,
            state_version=state_version,
            authority_epoch=self._epoch,
            outcome=outcome,
            admitted_at=now,
            confirmation_id=used_confirmation,
            reason=failure_reason,
            request_fingerprint=request_fingerprint,
        )
        self._admissions[aid] = record
        if outcome == "ADMITTED" and used_confirmation is not None:
            self._used_confirmations.add(used_confirmation)
        return record

    def dispatch_allowed(
        self,
        admission_id: str,
        *,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        now: str,
    ) -> tuple[bool, str]:
        record = self._admissions.get(_text(admission_id, name="admission_id"))
        if record is None:
            return False, "admission_missing"
        if record.outcome != "ADMITTED":
            return False, "admission_not_admitted"
        if record.intent_hash != _text(intent_hash, name="intent_hash"):
            return False, "intent_hash_changed"
        scope = (
            _text(account_id, name="account_id"),
            _text(environment, name="environment").upper(),
            InstrumentVersionIdentity(instrument_id, instrument_version),
            _text(action, name="action").upper(),
        )
        recorded_scope = (
            record.account_id,
            record.environment,
            record.instrument_version,
            record.action,
        )
        if scope != recorded_scope:
            return False, "admission_scope_changed"
        policy = self._policies[record.policy_id]
        active, reason = self._policy_active(policy, now)
        if not active:
            return False, reason
        if record.confirmation_id is not None:
            confirmation = self._confirmations[record.confirmation_id]
            if _instant(now, name="now") >= _instant(confirmation.expires_at, name="confirmation.expires_at"):
                return False, "confirmation_expired"
        return True, "allowed"

    def dispatch_guard(
        self,
        admission_id: str,
        *,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
    ) -> Callable[[str, str], tuple[bool, str]]:
        """Bind one immutable admission scope to the dispatcher's final barrier."""
        aid = _text(admission_id, name="admission_id")
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        identity = InstrumentVersionIdentity(instrument_id, instrument_version)
        normalized_action = _text(action, name="action").upper()

        def check(intent_hash: str, now: str) -> tuple[bool, str]:
            return self.dispatch_allowed(
                aid,
                intent_hash=intent_hash,
                account_id=account,
                environment=env,
                instrument_id=identity.instrument_id,
                instrument_version=identity.version,
                action=normalized_action,
                now=now,
            )

        return check


    def export_state(self) -> dict:
        """Return a canonical JSON-compatible snapshot of authority state.

        This is persistence data, not a cryptographic trust boundary.  The
        durable journal is responsible for integrity and ordering; restore()
        revalidates the financial authority identities and references.
        """
        def instrument(value: InstrumentVersionIdentity) -> dict:
            return {"instrument_id": value.instrument_id, "version": value.version}

        policies = []
        for policy_id in sorted(self._policies):
            policy = self._policies[policy_id]
            policies.append(
                {
                    "policy_id": policy.policy_id,
                    "account_id": policy.account_id,
                    "environments": sorted(policy.environments),
                    "instruments": [
                        instrument(item) for item in sorted(policy.instruments)
                    ],
                    "actions": sorted(policy.actions),
                    "max_notional": str(policy.max_notional),
                    "expires_at": policy.expires_at,
                    "autonomous": policy.autonomous,
                    "valid_from": policy.valid_from,
                    "protection_only": policy.protection_only,
                }
            )

        confirmations = []
        for confirmation_id in sorted(self._confirmations):
            confirmation = self._confirmations[confirmation_id]
            confirmations.append(
                {
                    "confirmation_id": confirmation.confirmation_id,
                    "policy_id": confirmation.policy_id,
                    "intent_hash": confirmation.intent_hash,
                    "account_id": confirmation.account_id,
                    "environment": confirmation.environment,
                    "instrument": instrument(confirmation.instrument_version),
                    "action": confirmation.action,
                    "notional": str(confirmation.notional),
                    "expires_at": confirmation.expires_at,
                }
            )

        admissions = []
        for admission_id in sorted(self._admissions):
            record = self._admissions[admission_id]
            admissions.append(
                {
                    "admission_id": record.admission_id,
                    "policy_id": record.policy_id,
                    "intent_hash": record.intent_hash,
                    "account_id": record.account_id,
                    "environment": record.environment,
                    "instrument": instrument(record.instrument_version),
                    "action": record.action,
                    "notional": str(record.notional),
                    "risk_reducing": record.risk_reducing,
                    "state_version": record.state_version,
                    "authority_epoch": record.authority_epoch,
                    "outcome": record.outcome,
                    "admitted_at": record.admitted_at,
                    "confirmation_id": record.confirmation_id,
                    "reason": record.reason,
                    "request_fingerprint": record.request_fingerprint,
                }
            )

        return {
            "schema_version": 1,
            "epoch": self._epoch,
            "policies": policies,
            "revocations": [
                {
                    "policy_id": policy_id,
                    "reason": value[0],
                    "revoked_at": value[1],
                }
                for policy_id, value in sorted(self._revocations.items())
            ],
            "confirmations": confirmations,
            "used_confirmations": sorted(self._used_confirmations),
            "admissions": admissions,
        }

    @classmethod
    def restore(cls, state: dict) -> "AuthorityService":
        """Rebuild authority state from one durable snapshot, fail closed."""
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise ValueError("unsupported authority state schema")
        service = cls()

        policies = state.get("policies")
        revocations = state.get("revocations")
        confirmations = state.get("confirmations")
        admissions = state.get("admissions")
        used_confirmations = state.get("used_confirmations")
        if not all(
            isinstance(value, list)
            for value in (
                policies,
                revocations,
                confirmations,
                admissions,
                used_confirmations,
            )
        ):
            raise ValueError("authority state collections must be lists")

        seen_policy_ids: set[str] = set()
        for item in policies:
            if not isinstance(item, dict):
                raise ValueError("policy snapshot entry must be an object")
            instruments = item.get("instruments")
            if not isinstance(instruments, list):
                raise ValueError("policy instruments must be a list")
            policy = AuthorityPolicy.create(
                policy_id=item.get("policy_id"),
                account_id=item.get("account_id"),
                environments=item.get("environments"),
                instruments=[
                    InstrumentVersionIdentity(
                        entry.get("instrument_id"), entry.get("version")
                    )
                    for entry in instruments
                    if isinstance(entry, dict)
                ],
                actions=item.get("actions"),
                max_notional=item.get("max_notional"),
                expires_at=item.get("expires_at"),
                autonomous=item.get("autonomous"),
                valid_from=item.get("valid_from"),
                protection_only=item.get("protection_only"),
            )
            if policy.policy_id in seen_policy_ids:
                raise AuthorityConflict("duplicate policy in authority snapshot")
            seen_policy_ids.add(policy.policy_id)
            service.register_policy(policy)

        for item in revocations:
            if not isinstance(item, dict):
                raise ValueError("revocation snapshot entry must be an object")
            service.revoke_policy(
                item.get("policy_id"),
                reason=item.get("reason"),
                revoked_at=item.get("revoked_at"),
            )

        seen_confirmation_ids: set[str] = set()
        for item in confirmations:
            if not isinstance(item, dict) or not isinstance(item.get("instrument"), dict):
                raise ValueError("confirmation snapshot entry is invalid")
            instrument = item["instrument"]
            cid = item.get("confirmation_id")
            if cid in seen_confirmation_ids:
                raise AuthorityConflict("duplicate confirmation in authority snapshot")
            seen_confirmation_ids.add(cid)
            service.add_confirmation(
                confirmation_id=cid,
                policy_id=item.get("policy_id"),
                intent_hash=item.get("intent_hash"),
                account_id=item.get("account_id"),
                environment=item.get("environment"),
                instrument_id=instrument.get("instrument_id"),
                instrument_version=instrument.get("version"),
                action=item.get("action"),
                notional=item.get("notional"),
                expires_at=item.get("expires_at"),
            )

        restored_admissions: dict[str, AdmissionRecord] = {}
        derived_used: set[str] = set()
        for item in admissions:
            if not isinstance(item, dict) or not isinstance(item.get("instrument"), dict):
                raise ValueError("admission snapshot entry is invalid")
            admission_id = _text(item.get("admission_id"), name="admission_id")
            if admission_id in restored_admissions:
                raise AuthorityConflict("duplicate admission in authority snapshot")
            policy_id = _text(item.get("policy_id"), name="policy_id")
            if policy_id not in service._policies:
                raise ValueError("admission references missing policy")
            instrument = item["instrument"]
            identity = InstrumentVersionIdentity(
                instrument.get("instrument_id"), instrument.get("version")
            )
            amount = _decimal(item.get("notional"), name="notional")
            if amount < 0:
                raise ValueError("admission notional must be non-negative")
            state_version = item.get("state_version")
            authority_epoch = item.get("authority_epoch")
            risk_reducing = item.get("risk_reducing")
            if (
                not isinstance(state_version, int)
                or isinstance(state_version, bool)
                or state_version < 0
            ):
                raise ValueError("admission state_version is invalid")
            if (
                not isinstance(authority_epoch, int)
                or isinstance(authority_epoch, bool)
                or authority_epoch < 0
                or authority_epoch > service._epoch
            ):
                raise ValueError("admission authority_epoch is invalid")
            if not isinstance(risk_reducing, bool):
                raise TypeError("admission risk_reducing must be boolean")
            outcome = _text(item.get("outcome"), name="outcome").upper()
            if outcome not in {"ADMITTED", "REJECTED"}:
                raise ValueError("admission outcome is invalid")
            admitted_at = _text(item.get("admitted_at"), name="admitted_at")
            _instant(admitted_at, name="admitted_at")
            confirmation_id = item.get("confirmation_id")
            if confirmation_id is not None:
                confirmation_id = _text(
                    confirmation_id, name="confirmation_id"
                )
                if confirmation_id not in service._confirmations:
                    raise ValueError("admission references missing confirmation")
                if outcome != "ADMITTED":
                    raise ValueError("rejected admission cannot consume confirmation")
                derived_used.add(confirmation_id)
            record = AdmissionRecord(
                admission_id=admission_id,
                policy_id=policy_id,
                intent_hash=_text(item.get("intent_hash"), name="intent_hash"),
                account_id=_text(item.get("account_id"), name="account_id"),
                environment=_text(item.get("environment"), name="environment").upper(),
                instrument_version=identity,
                action=_text(item.get("action"), name="action").upper(),
                notional=amount,
                risk_reducing=risk_reducing,
                state_version=state_version,
                authority_epoch=authority_epoch,
                outcome=outcome,
                admitted_at=admitted_at,
                confirmation_id=confirmation_id,
                reason=_text(item.get("reason"), name="reason"),
                request_fingerprint=_text(
                    item.get("request_fingerprint"), name="request_fingerprint"
                ),
            )
            restored_admissions[admission_id] = record

        normalized_used = {
            _text(value, name="used_confirmation")
            for value in used_confirmations
        }
        if normalized_used != derived_used:
            raise ValueError("used confirmation set does not match admitted records")
        if not normalized_used <= set(service._confirmations):
            raise ValueError("used confirmation references missing confirmation")

        epoch = state.get("epoch")
        if (
            not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or epoch != service._epoch
        ):
            raise ValueError("authority epoch does not match durable mutations")
        service._admissions = restored_admissions
        service._used_confirmations = normalized_used
        return service
