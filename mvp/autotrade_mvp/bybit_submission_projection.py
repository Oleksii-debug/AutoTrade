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

    if type(book) is not DurableOrderBookProjection:
        raise TypeError("book must be exact DurableOrderBookProjection")

    prepared = guarded_order_projection(prepared_request)
    provider_id = book.provider_id
    account_id = book.account_id
    environment = book.environment
    if provider_id != "BYBIT":
        raise BybitSubmissionProjectionError(
            "authenticated Bybit submission requires BYBIT order projection"
        )
    if (
        prepared["account_id"] != account_id
        or prepared["environment"] != environment
    ):
        raise BybitSubmissionProjectionError(
            "Bybit prepared request scope differs from durable order projection"
        )

    body = prepared["body"]
    if type(body) is not dict:
        # guarded_order_projection currently returns one exact detached dict;
        # fail closed if that canonical surface changes.
        raise BybitSubmissionProjectionError(
            "Bybit prepared request body projection is non-canonical"
        )
    client_order_id = body.get("orderLinkId")
    if type(client_order_id) is not str or not client_order_id:
        raise BybitSubmissionProjectionError(
            "Bybit prepared request lacks canonical client order identity"
        )
    # Require the target order before touching provider-result state.
    book.order(client_order_id)

    # First retain the generic send truth.  Exact response bytes alone are still
    # UNKNOWN here; a crash or any failure below can only leave this safe state.
    generic_results = book.sync_submission_attempt(attempt_id=attempt_id)
    if not generic_results:
        raise BybitSubmissionProjectionError(
            "submission attempt has no durable outbound send to normalize"
        )
    generic_result = generic_results[-1]

    store, store_identity = require_exact_order_projection_authority(book)
    with journal_store_authority_scope(store, store_identity):
        binding = load_submission_response_binding(
            store,
            environment=environment,
            account_id=account_id,
            attempt_id=attempt_id,
        )
    bound = submission_response_binding_projection(binding)
    if bound["terminal_state"] != "SENT":
        # UNKNOWN/opaque transport evidence is reconciliation-only and cannot
        # mint an authenticated ACK/REJECT lifecycle fact.
        return generic_result

    prepared_request_sha256 = guarded_order_request_sha256(prepared_request)
    observation = observe_submission_json_response(
        response_binding=binding,
        provider_id="BYBIT",
        endpoint=prepared["endpoint"],
        prepared_request_sha256=prepared_request_sha256,
        capability_snapshot_ids=tuple(prepared["capability_snapshot_ids"]),
        instrument_versions=tuple(prepared["instrument_versions"]),
    )
    normalized = parse_submission_response(
        attempt_id=attempt_id,
        prepared_request=prepared_request,
        observation=observation,
    )
    if normalized.get("client_order_id") != client_order_id:
        raise BybitSubmissionProjectionError(
            "normalized Bybit response changed client order identity"
        )

    outcome = normalized.get("outcome")
    if outcome == "UNKNOWN":
        # Provider-specific parsing still found no terminal lifecycle fact.
        # Preserve the already-durable generic UNKNOWN without adding a second
        # semantically empty order mutation.
        return generic_result
    if outcome not in {"ACKNOWLEDGED", "REJECTED"}:
        raise BybitSubmissionProjectionError(
            "Bybit submission normalizer returned unsupported lifecycle outcome"
        )

    observed = provider_submission_observation_projection(observation)
    evidence = normalized.get("evidence")
    if type(evidence) is not list or len(evidence) != 1 or type(evidence[0]) is not dict:
        raise BybitSubmissionProjectionError(
            "Bybit normalized lifecycle result lacks one canonical evidence reference"
        )
    evidence_ref = dict(evidence[0])
    if evidence_ref.get("sha256") != bound["response_sha256"]:
        raise BybitSubmissionProjectionError(
            "Bybit normalized evidence digest differs from durable response"
        )
    if evidence_ref.get("observed_at") != observed["sent_at"]:
        raise BybitSubmissionProjectionError(
            "Bybit normalized evidence time differs from durable response"
        )

    provider_order_id = normalized.get("provider_order_id")
    acknowledge_request = {
        "client_order_id": client_order_id,
        "provider_order_id": provider_order_id,
        "status": outcome,
        "attempt_id": attempt_id,
    }

    artifact_store = book.evidence_artifact_store
    if environment in {"PAPER", "LIVE"} and type(artifact_store) is not ArtifactStore:
        raise BybitSubmissionProjectionError(
            "authenticated Bybit lifecycle projection requires trusted ArtifactStore"
        )
    if artifact_store is not None:
        if type(artifact_store) is not ArtifactStore:
            raise BybitSubmissionProjectionError(
                "provider evidence ArtifactStore authority is non-canonical"
            )
        source_uri = evidence_ref.get("source_uri")
        rights_id = evidence_ref.get("rights_id")
        if type(source_uri) is not str or not source_uri:
            raise BybitSubmissionProjectionError(
                "Bybit normalized evidence source is non-canonical"
            )
        if type(rights_id) is not str or not rights_id:
            raise BybitSubmissionProjectionError(
                "Bybit normalized evidence rights id is non-canonical"
            )
        manifest = ArtifactStore.publish_bytes(
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
                "request_hash": payload_digest(acknowledge_request),
                "observed_at": evidence_ref["observed_at"],
                "rights_id": rights_id,
            },
        )
        if (
            manifest.get("artifact_id") != evidence_ref["artifact_id"]
            or manifest.get("sha256") != evidence_ref["sha256"]
        ):
            raise BybitSubmissionProjectionError(
                "published Bybit response evidence differs from normalized evidence"
            )

    return book.acknowledge(
        event_key="bybit-submission:" + observed["evidence_ref"],
        client_order_id=client_order_id,
        provider_order_id=provider_order_id,
        status=outcome,
        attempt_id=attempt_id,
        committed_at=evidence_ref["observed_at"],
        evidence_refs=(evidence_ref,),
    )
