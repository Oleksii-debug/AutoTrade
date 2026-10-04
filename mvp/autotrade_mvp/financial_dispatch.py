"""Canonical PAPER/LIVE composition for durable financial dispatch.

This seam closes the gap between WP-17 policy/confirmation authority and WP-18
persist-before-send dispatch.  Low-level ``GuardedDispatcher.dispatch`` remains
available for isolated replay/simulation and component tests; production
financial writes should enter through ``ProductionFinancialDispatcher`` so a
caller cannot substitute an arbitrary permissive authority callback or omit the
fresh final-barrier clock.

This module does not itself claim provider qualification or product readiness.
Those remain evidence/qualification responsibilities above this composition.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .authority import AuthorityConflict, AuthorityService, _authority_service_store
from .dispatch import (
    DispatchOutcome,
    GuardedDispatcher,
    SenderCheck,
    TransportSend,
    _identity_digest,
)
from .persistence import require_exact_journal_store_authority


def _utc_now_text() -> str:
    """Return a fresh UTC instant for the irreversible send barrier."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class ProductionFinancialDispatcher:
    """Bind one PAPER/LIVE dispatcher to one durable ``AuthorityService``.

    The binding is deliberately exact-type and exact-store.  The service that
    decides whether an admitted financial command is still allowed must observe
    the same physical journal generation that owns the submission attempt.
    Every call revalidates that composition immediately before dispatch.

    Unlike the low-level dispatcher, this API exposes no ``authority_check`` or
    ``final_barrier_clock`` argument.  It always mints the scoped guard from the
    canonical ``AuthorityService`` and uses a fresh UTC clock at the provider's
    final guard invocation.
    """

    __slots__ = ("_dispatcher", "_authority")

    def __init__(
        self,
        dispatcher: GuardedDispatcher,
        authority: AuthorityService,
    ) -> None:
        self._require_current_composition(dispatcher, authority)
        self._dispatcher = dispatcher
        self._authority = authority

    @staticmethod
    def _require_current_composition(
        dispatcher: GuardedDispatcher,
        authority: AuthorityService,
    ) -> None:
        if type(dispatcher) is not GuardedDispatcher:
            raise TypeError("production dispatcher must be the canonical GuardedDispatcher")
        if type(authority) is not AuthorityService:
            raise TypeError("production authority must be the canonical AuthorityService")
        if dispatcher.environment not in {"PAPER", "LIVE"}:
            raise ValueError("production financial dispatch permits only PAPER or LIVE")

        # GuardedDispatcher's scope key is frozen from constructor scope.  Its
        # public attributes are intentionally simple data today, so reject an
        # in-process account/environment retarget before deriving authority.
        expected_scope_key = _identity_digest(
            dispatcher.environment,
            dispatcher.account_id,
        )
        if dispatcher.scope_key != expected_scope_key:
            raise PermissionError("production dispatch scope changed after construction")

        dispatch_store = GuardedDispatcher._journal_store_authority(dispatcher)
        authority_store = _authority_service_store(authority, required=True)
        dispatch_identity = require_exact_journal_store_authority(
            dispatch_store,
            subject="production dispatch journal store",
        )
        authority_identity = require_exact_journal_store_authority(
            authority_store,
            subject="production authority journal store",
        )
        if dispatch_identity != authority_identity:
            raise AuthorityConflict(
                "production dispatcher and AuthorityService must share one durable journal authority"
            )

    def dispatch(
        self,
        *,
        admission_id: str,
        attempt_id: str,
        intent_id: str,
        intent_hash: str,
        provider: str,
        request: Mapping[str, Any],
        now: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        transport_send: TransportSend,
        sender_check: SenderCheck,
        capability_snapshot_id: str | None = None,
        client_id_max_length: int = 32,
        client_id_format: str = "TOKEN",
        submission_scope: Mapping[str, Any] | None = None,
    ) -> DispatchOutcome:
        """Perform one canonical PAPER/LIVE financial dispatch attempt."""

        if not callable(transport_send):
            raise TypeError("transport_send must be callable")
        if not callable(sender_check):
            raise TypeError("sender_check must be callable")

        dispatcher = self._dispatcher
        authority = self._authority
        self._require_current_composition(dispatcher, authority)

        # Call the canonical class method unbound after exact-type validation so
        # an instance-attached replacement cannot mint a weaker production
        # guard. dispatch_allowed revalidates admission/risk/reservation and the
        # durable authority linearization point on both dispatcher checks.
        authority_check = AuthorityService.dispatch_guard(
            authority,
            admission_id,
            account_id=dispatcher.account_id,
            environment=dispatcher.environment,
            instrument_id=instrument_id,
            instrument_version=instrument_version,
            action=action,
            capability_snapshot_id=capability_snapshot_id,
        )

        # Keep the low-level dispatcher as the sole send-state owner.  The fresh
        # clock is intentionally not caller-selectable: signing/quota work may
        # take time before provider transports invoke final_guard(), and expired
        # authority must be observed at that irreversible boundary.
        return GuardedDispatcher.dispatch(
            dispatcher,
            attempt_id=attempt_id,
            intent_id=intent_id,
            intent_hash=intent_hash,
            provider=provider,
            request=request,
            now=now,
            authority_check=authority_check,
            transport_send=transport_send,
            client_id_max_length=client_id_max_length,
            client_id_format=client_id_format,
            final_barrier_clock=_utc_now_text,
            sender_check=sender_check,
            submission_scope=submission_scope,
        )
