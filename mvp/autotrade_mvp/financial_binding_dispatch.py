"""Compose one durable admitted request identity into GuardedDispatcher.

This is a narrow current-head successor to the canonical WP-18/#1099
prepared-scope semantics.  It does not create a second dispatcher or transport.
The canonical GuardedDispatcher still owns Prepared -> Sending -> Sent/Unknown
chronology and no-blind-retry behavior.

The composer removes caller authority over provider/account/intent/client-id and
submission-scope identity.  It resolves those values from the durable ADMITTED
financial request binding and exact admission history, proves that the supplied
request bytes match the bound request digest, constructs one canonical
submission scope, and lets the existing dispatcher persist that scope in
SubmissionPrepared.

PAPER/LIVE is intentionally fail-closed here.  Production dispatch must consume
the separately owned sealed product financial authority, current C/Q/account
truth and recovery-issued one-use sender permit before the first irreversible
byte.  A generic callable authority check or a durable content binding alone is
not production trading authority.
"""

from __future__ import annotations

from hashlib import sha256
import re
from typing import Any, Callable, Mapping
from uuid import UUID

from .authority import AuthorityConflict, AuthorityService
from .dispatch import (
    DispatchOutcome,
    GuardedDispatcher,
    stable_client_order_id,
)
from .durable_financial_request_binding import (
    DurableFinancialRequestBindingError,
    DurableFinancialRequestBindingRegistry,
)
from .financial_request_binding import FinancialRequestBindingMaterial
from .persistence import JournalStore, canonical_json


class FinancialBindingDispatchError(RuntimeError):
    """Raised when durable financial identity cannot enter guarded dispatch."""


_FINANCIAL_SCOPE_SCHEMA = "financial-submission-scope.v1"
_BINDING_ID = re.compile(r"^financial-request:sha256:[0-9a-f]{64}$")


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise FinancialBindingDispatchError(
            f"{name} must be exact canonical non-empty text"
        )
    return value


def _sha256_json(value: object) -> str:
    return "sha256:" + sha256(canonical_json(value).encode("utf-8")).hexdigest()


def financial_submission_scope(
    *,
    admission_id: str,
    material: FinancialRequestBindingMaterial,
) -> dict[str, object]:
    """Return the non-circular scope whose digest is sealed by the material.

    The material binding id itself includes ``submission_scope_digest``, so the
    scope cannot contain the final binding id without a circular hash.  The
    admission id plus the immutable component identities below uniquely select
    the durable registry entry; a reader can resolve that entry and prove its
    final binding id independently.
    """

    aid = _text(admission_id, name="admission_id")
    if type(material) is not FinancialRequestBindingMaterial:
        raise TypeError("material must be exact FinancialRequestBindingMaterial")
    return {
        "schema_version": _FINANCIAL_SCOPE_SCHEMA,
        "admission_id": aid,
        "risk_snapshot_id": material.risk_snapshot_id,
        "risk_decision_id": material.risk_decision_id,
        "reservation_id": material.reservation_id,
        "provider_scope_digest": material.provider_scope_digest,
        "capability_snapshot_id": material.capability_snapshot_id,
        "qualification_identity_digest": material.qualification_identity_digest,
        "request_sha256": material.request_sha256,
    }


def financial_submission_scope_digest(
    *,
    admission_id: str,
    material: FinancialRequestBindingMaterial,
) -> str:
    return _sha256_json(
        financial_submission_scope(
            admission_id=admission_id,
            material=material,
        )
    )


def _client_order_parameters(material: FinancialRequestBindingMaterial) -> tuple[int, str]:
    """Infer only canonical GuardedDispatcher client-id encodings.

    This does not let a caller select a format.  UUID and TOKEN are the two
    canonical stable-client-id encodings already owned by dispatch.py.
    """

    client_id = material.client_order_id
    try:
        if str(UUID(client_id)) == client_id:
            return 36, "UUID"
    except (ValueError, TypeError, AttributeError):
        pass
    if client_id.startswith("at-") and len(client_id) >= 20:
        return len(client_id), "TOKEN"
    raise FinancialBindingDispatchError(
        "durable client_order_id is not a canonical GuardedDispatcher stable id"
    )


def _load_admission(
    store: JournalStore,
    admission_id: str,
):
    try:
        authority = AuthorityService(store)
    except (AuthorityConflict, TypeError, ValueError, RuntimeError) as error:
        raise FinancialBindingDispatchError(
            "canonical financial admission history cannot be reconstructed"
        ) from error
    record = authority._admissions.get(admission_id)
    if record is None or record.outcome != "ADMITTED":
        raise FinancialBindingDispatchError(
            "guarded financial dispatch requires exact ADMITTED history"
        )
    if record.intent_id is None:
        raise FinancialBindingDispatchError(
            "admitted financial history lacks durable intent identity"
        )
    return record


def _require_prepared_chronology(
    dispatcher: GuardedDispatcher,
    *,
    attempt_id: str,
    scope: Mapping[str, object],
    scope_digest: str,
    material: FinancialRequestBindingMaterial,
) -> None:
    events = dispatcher._events(attempt_id)
    if not events:
        raise FinancialBindingDispatchError(
            "guarded dispatch produced no durable submission chronology"
        )
    prepared = events[0]
    payload = prepared.get("payload")
    if (
        prepared.get("event_type") != "SubmissionPrepared"
        or type(payload) is not dict
        or payload.get("submission_scope") != dict(scope)
        or payload.get("submission_scope_hash") != scope_digest
        or payload.get("request_hash") != material.request_sha256
        or payload.get("client_order_id") != material.client_order_id
        or payload.get("provider") != material.provider_id
        or payload.get("account_id") != material.account_id
        or payload.get("environment") != material.runtime_environment
    ):
        raise FinancialBindingDispatchError(
            "SubmissionPrepared differs from durable financial request binding"
        )
    # Every later event belongs to the same aggregate/version chain.  The exact
    # binding is therefore recovered from Prepared rather than trusted from a
    # free field on a later row.  Current #1046/#1099 production convergence may
    # additionally repeat the binding on Sending/terminal rows when it owns that
    # overlapping dispatch.py mutation.
    aggregate_id = prepared.get("aggregate_id")
    for index, event in enumerate(events, 1):
        if (
            event.get("aggregate_id") != aggregate_id
            or event.get("aggregate_type") != "submission_attempt"
            or event.get("aggregate_version") != index
        ):
            raise FinancialBindingDispatchError(
                "submission chronology does not retain one prepared binding aggregate"
            )


def dispatch_durable_financial_request(
    dispatcher: GuardedDispatcher,
    *,
    admission_id: str,
    attempt_id: str,
    request: Mapping[str, Any],
    now: str,
    authority_check: Callable[[str, str], tuple[bool, str]],
    transport_send: Callable[[str, Mapping[str, Any], Callable[[], None]], Any],
) -> DispatchOutcome:
    """Dispatch exactly the request sealed by one durable ADMITTED binding.

    The generic callable seams are accepted only in SIMULATION.  PAPER/LIVE
    requires the sealed product-owned authority/current-Q/recovery-sender union
    tracked by #987/#1099/#1046 and is deliberately zero-wire here.
    """

    if type(dispatcher) is not GuardedDispatcher:
        raise TypeError("dispatcher must be exact GuardedDispatcher")
    if dispatcher.environment != "SIMULATION":
        raise FinancialBindingDispatchError(
            "PAPER/LIVE financial dispatch requires sealed production authority"
        )
    aid = _text(admission_id, name="admission_id")
    attempt = _text(attempt_id, name="attempt_id")
    if not isinstance(request, Mapping):
        raise TypeError("request must be a mapping")
    if not callable(authority_check) or not callable(transport_send):
        raise TypeError("authority_check and transport_send must be callable")

    try:
        store = dispatcher._journal_store_authority()
    except (PermissionError, TypeError, ValueError, RuntimeError) as error:
        raise FinancialBindingDispatchError(
            "dispatcher JournalStore authority is unavailable"
        ) from error
    if type(store) is not JournalStore:
        raise FinancialBindingDispatchError(
            "financial binding dispatch requires exact JournalStore authority"
        )
    try:
        material = DurableFinancialRequestBindingRegistry(store).resolve(aid)
    except DurableFinancialRequestBindingError as error:
        raise FinancialBindingDispatchError(
            "durable admitted provider request binding is unavailable"
        ) from error
    if _BINDING_ID.fullmatch(material.binding_id) is None:
        raise FinancialBindingDispatchError(
            "durable financial request binding id is non-canonical"
        )

    record = _load_admission(store, aid)
    if (
        dispatcher.account_id != material.account_id
        or dispatcher.environment != material.runtime_environment
        or record.account_id != material.account_id
        or record.environment != material.runtime_environment
    ):
        raise FinancialBindingDispatchError(
            "dispatcher scope differs from durable financial binding"
        )

    request_dict = dict(request)
    request_hash = _sha256_json(request_dict)
    if request_hash != material.request_sha256:
        raise FinancialBindingDispatchError(
            "request bytes differ from durable financial binding"
        )

    scope = financial_submission_scope(admission_id=aid, material=material)
    scope_digest = _sha256_json(scope)
    if scope_digest != material.submission_scope_digest:
        raise FinancialBindingDispatchError(
            "canonical submission scope differs from durable financial binding"
        )

    client_id_max_length, client_id_format = _client_order_parameters(material)
    expected_client_id = stable_client_order_id(
        material.provider_id,
        record.intent_id,
        environment=material.runtime_environment,
        account_id=material.account_id,
        max_length=client_id_max_length,
        client_id_format=client_id_format,
    )
    if expected_client_id != material.client_order_id:
        raise FinancialBindingDispatchError(
            "durable client_order_id differs from admitted intent identity"
        )

    outcome = dispatcher.dispatch(
        attempt_id=attempt,
        intent_id=record.intent_id,
        intent_hash=record.intent_hash,
        provider=material.provider_id,
        request=request_dict,
        now=now,
        authority_check=authority_check,
        transport_send=transport_send,
        client_id_max_length=client_id_max_length,
        client_id_format=client_id_format,
        submission_scope=scope,
    )
    _require_prepared_chronology(
        dispatcher,
        attempt_id=attempt,
        scope=scope,
        scope_digest=scope_digest,
        material=material,
    )
    return outcome
