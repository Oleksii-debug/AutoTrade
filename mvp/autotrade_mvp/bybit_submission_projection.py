"""Authenticated Bybit submission normalization into the durable order projection.

The generic WP-18 submission journal proves that one exact response belongs to
one exact send, but it deliberately does not interpret provider lifecycle
semantics.  This adapter composes the existing durable response binding,
provider-specific Bybit normalizer and the single durable OMS.  It creates no
second order authority and never resends.
"""
from __future__ import annotations

from autotrade_runtime.artifacts import ArtifactStore

from .bybit_v5 import (
    BybitPreparedSubmission,
    guarded_order_projection,
    guarded_order_request_sha256,
    parse_submission_response,
)
from .dispatch import (
    load_submission_response_binding,
    submission_response_binding_projection,
)
from .durable_order_projection import (
    DurableOrderBookProjection,
    DurableOrderMutationResult,
    require_exact_order_projection_authority,
)
from .order_projection import OrderProjectionConflict
from .persistence import (
    journal_store_authority_scope,
    payload_digest,
)
from .provider_core import (
    ProviderCoreError,
    observe_submission_json_response,
    provider_submission_observation_projection,
)


class BybitSubmissionProjectionError(OrderProjectionConflict):
    """Raised when authenticated Bybit submission evidence cannot be projected."""


def _bind_authenticated_bybit_submission_projection():
    """Seal executable dependencies used by provider-write lifecycle projection."""

    _artifact_store_type = ArtifactStore
    _artifact_publish_bytes = ArtifactStore.publish_bytes
    _guarded_order_projection = guarded_order_projection
    _guarded_order_request_sha256 = guarded_order_request_sha256
    _parse_submission_response = parse_submission_response
    _load_submission_response_binding = load_submission_response_binding
    _submission_response_binding_projection = submission_response_binding_projection
    _require_exact_order_projection_authority = require_exact_order_projection_authority
    _journal_store_authority_scope = journal_store_authority_scope
    _payload_digest = payload_digest
    _observe_submission_json_response = observe_submission_json_response
    _provider_submission_observation_projection = provider_submission_observation_projection
    _book_type = DurableOrderBookProjection
    _book_order = DurableOrderBookProjection.order
    _book_sync_submission_attempt = DurableOrderBookProjection.sync_submission_attempt
    _book_acknowledge = DurableOrderBookProjection.acknowledge
    _error_type = BybitSubmissionProjectionError
    _type_error = TypeError
    _type = type
    _len = len
    _dict_type = dict
    _list_type = list
    _tuple_type = tuple
    _str_type = str
    _object_getattribute = object.__getattribute__
    _getattr = getattr

    _code_authorities = _tuple_type(
        (_callable, _getattr(_callable, "__code__", None))
        for _callable in (
            _artifact_publish_bytes,
            _guarded_order_projection,
            _guarded_order_request_sha256,
            _parse_submission_response,
            _load_submission_response_binding,
            _submission_response_binding_projection,
            _require_exact_order_projection_authority,
            _journal_store_authority_scope,
            _payload_digest,
            _observe_submission_json_response,
            _provider_submission_observation_projection,
            _book_order,
            _book_sync_submission_attempt,
            _book_acknowledge,
        )
    )

    def _require_dependency_authority() -> None:
        for _callable, _expected_code in _code_authorities:
            if (
                _expected_code is not None
                and _getattr(_callable, "__code__", None) is not _expected_code
            ):
                raise _error_type(
                    "authenticated Bybit projection executable authority changed"
                )

    def project_authenticated_bybit_submission(
        book: DurableOrderBookProjection,
        *,
        attempt_id: str,
        prepared_request: BybitPreparedSubmission,
    ) -> DurableOrderMutationResult:
        """Resolve one already-sent Bybit attempt without resending.
    
        The provider-neutral submission projection is applied first.  Therefore any
        later parser, evidence-publication or ACK commit failure leaves the durable
        OMS at UNKNOWN instead of silently restoring send authority.
        """
    
        _require_dependency_authority()
        if _type(book) is not _book_type:
            raise _type_error("book must be exact DurableOrderBookProjection")
    
        # Establish the exact durable OMS selection authority before any caller-
        # replaceable method dispatch or authority-bearing scope read.
        store, store_identity = _require_exact_order_projection_authority(book)
        book_state = _object_getattribute(book, "__dict__")
        provider_id = book_state["provider_id"]
        account_id = book_state["account_id"]
        environment = book_state["environment"]
        artifact_store = book_state["evidence_artifact_store"]
    
        prepared = _guarded_order_projection(prepared_request)
        if provider_id != "BYBIT":
            raise _error_type(
                "authenticated Bybit submission requires BYBIT order projection"
            )
        if (
            prepared["account_id"] != account_id
            or prepared["environment"] != environment
        ):
            raise _error_type(
                "Bybit prepared request scope differs from durable order projection"
            )
    
        body = prepared["body"]
        if _type(body) is not _dict_type:
            # guarded_order_projection currently returns one exact detached dict;
            # fail closed if that canonical surface changes.
            raise _error_type(
                "Bybit prepared request body projection is non-canonical"
            )
        client_order_id = body.get("orderLinkId")
        if _type(client_order_id) is not _str_type or not client_order_id:
            raise _error_type(
                "Bybit prepared request lacks canonical client order identity"
            )
        # Require the target order before touching provider-result state.
        _book_order(book, client_order_id)
    
        # First retain the generic send truth.  Exact response bytes alone are still
        # UNKNOWN here; a crash or any failure below can only leave this safe state.
        generic_results = _book_sync_submission_attempt(book, attempt_id=attempt_id)
        if not generic_results:
            raise _error_type(
                "submission attempt has no durable outbound send to normalize"
            )
        generic_result = generic_results[-1]
    
        with _journal_store_authority_scope(store, store_identity):
            binding = _load_submission_response_binding(
                store,
                environment=environment,
                account_id=account_id,
                attempt_id=attempt_id,
            )
        bound = _submission_response_binding_projection(binding)
        if bound["terminal_state"] != "SENT":
            # UNKNOWN/opaque transport evidence is reconciliation-only and cannot
            # mint an authenticated ACK/REJECT lifecycle fact.
            return generic_result
    
        prepared_request_sha256 = _guarded_order_request_sha256(prepared_request)
        observation = _observe_submission_json_response(
            response_binding=binding,
            provider_id="BYBIT",
            endpoint=prepared["endpoint"],
            prepared_request_sha256=prepared_request_sha256,
            capability_snapshot_ids=_tuple_type(prepared["capability_snapshot_ids"]),
            instrument_versions=_tuple_type(prepared["instrument_versions"]),
        )
        normalized = _parse_submission_response(
            attempt_id=attempt_id,
            prepared_request=prepared_request,
            observation=observation,
        )
        if _type(normalized) is not _dict_type:
            raise _error_type(
                "Bybit submission normalizer returned non-canonical result"
            )
        if normalized.get("client_order_id") != client_order_id:
            raise _error_type(
                "normalized Bybit response changed client order identity"
            )
    
        outcome = normalized.get("outcome")
        if outcome == "UNKNOWN":
            # Provider-specific parsing still found no terminal lifecycle fact.
            # Preserve the already-durable generic UNKNOWN without adding a second
            # semantically empty order mutation.
            return generic_result
        if outcome not in {"ACKNOWLEDGED", "REJECTED"}:
            raise _error_type(
                "Bybit submission normalizer returned unsupported lifecycle outcome"
            )
    
        observed = _provider_submission_observation_projection(observation)
        evidence = normalized.get("evidence")
        if (
            _type(evidence) is not _list_type
            or _len(evidence) != 1
            or _type(evidence[0]) is not _dict_type
        ):
            raise _error_type(
                "Bybit normalized lifecycle result lacks one canonical evidence reference"
            )
        evidence_ref = _dict_type(evidence[0])
        if evidence_ref.get("sha256") != bound["response_sha256"]:
            raise _error_type(
                "Bybit normalized evidence digest differs from durable response"
            )
        if evidence_ref.get("observed_at") != observed["sent_at"]:
            raise _error_type(
                "Bybit normalized evidence time differs from durable response"
            )
    
        provider_order_id = normalized.get("provider_order_id")
        acknowledge_request = {
            "client_order_id": client_order_id,
            "provider_order_id": provider_order_id,
            "status": outcome,
            "attempt_id": attempt_id,
        }
    
        if environment in {"PAPER", "LIVE"} and _type(artifact_store) is not _artifact_store_type:
            raise _error_type(
                "authenticated Bybit lifecycle projection requires trusted ArtifactStore"
            )
        if artifact_store is not None:
            if _type(artifact_store) is not _artifact_store_type:
                raise _error_type(
                    "provider evidence ArtifactStore authority is non-canonical"
                )
            source_uri = evidence_ref.get("source_uri")
            rights_id = evidence_ref.get("rights_id")
            if _type(source_uri) is not _str_type or not source_uri:
                raise _error_type(
                    "Bybit normalized evidence source is non-canonical"
                )
            if _type(rights_id) is not _str_type or not rights_id:
                raise _error_type(
                    "Bybit normalized evidence rights id is non-canonical"
                )
            manifest = _artifact_publish_bytes(
                artifact_store,
                artifact_id=evidence_ref["artifact_id"],
                data=bound["response_bytes"],
                media_type="application/json",
                rights={"storage": True, "export": False},
                source_refs=[source_uri],
                metadata={
                    "provider_id": "BYBIT",
                    "account_id": account_id,
                    "environment": environment,
                    "order_operation": "ACKNOWLEDGE",
                    "request_hash": _payload_digest(acknowledge_request),
                    "observed_at": evidence_ref["observed_at"],
                    "rights_id": rights_id,
                },
            )
            if _type(manifest) is not _dict_type:
                raise _error_type(
                    "provider evidence publication returned non-canonical manifest"
                )
            if (
                manifest.get("artifact_id") != evidence_ref["artifact_id"]
                or manifest.get("sha256") != evidence_ref["sha256"]
            ):
                raise _error_type(
                    "published Bybit response evidence differs from normalized evidence"
                )
    
        return _book_acknowledge(
            book,
            event_key="bybit-submission:" + observed["evidence_ref"],
            client_order_id=client_order_id,
            provider_order_id=provider_order_id,
            status=outcome,
            attempt_id=attempt_id,
            committed_at=evidence_ref["observed_at"],
            evidence_refs=(evidence_ref,),
        )

    return project_authenticated_bybit_submission


project_authenticated_bybit_submission = _bind_authenticated_bybit_submission_projection()
del _bind_authenticated_bybit_submission_projection
