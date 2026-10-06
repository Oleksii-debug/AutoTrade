"""Authenticated Bybit submission normalization into the durable order projection.

The generic WP-18 submission journal proves that one exact response belongs to
one exact send, but it deliberately does not interpret provider lifecycle
semantics. This adapter composes the existing durable response binding,
provider-specific Bybit normalizer and the single durable OMS. It creates no
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
from .persistence import journal_store_authority_scope, payload_digest
from .provider_core import (
    observe_submission_json_response,
    provider_submission_observation_projection,
)


class BybitSubmissionProjectionError(OrderProjectionConflict):
    """Authenticated Bybit submission evidence cannot be projected safely."""


def _install_authenticated_bybit_submission_projector(
    *,
    book_type,
    prepared_type,
    prepared_projection,
    request_digest_projection,
    response_parser,
    binding_loader,
    binding_projection,
    book_authority,
    store_authority_scope,
    observation_builder,
    observation_projection,
    artifact_type,
    digest_function,
):
    """Capture every post-transport authority dependency outside module aliases."""

    prepared_projection_code = prepared_projection.__code__
    request_digest_code = request_digest_projection.__code__
    response_parser_code = response_parser.__code__
    binding_loader_code = binding_loader.__code__
    binding_projection_code = binding_projection.__code__
    book_authority_code = book_authority.__code__
    store_scope_code = store_authority_scope.__code__
    observation_builder_code = observation_builder.__code__
    observation_projection_code = observation_projection.__code__
    digest_code = digest_function.__code__

    book_order = book_type.order
    book_order_code = book_order.__code__
    book_sync = book_type.sync_submission_attempt
    book_sync_code = book_sync.__code__
    book_acknowledge = book_type.acknowledge
    book_acknowledge_code = book_acknowledge.__code__
    artifact_publish = artifact_type.publish_bytes
    artifact_publish_code = artifact_publish.__code__

    canonical_type = type
    canonical_dict = dict
    canonical_list = list
    canonical_tuple = tuple
    canonical_str = str
    canonical_len = len
    canonical_object = object
    object_getattribute = canonical_object.__getattribute__
    type_error = TypeError
    projection_error_type = BybitSubmissionProjectionError

    def implementation_changed() -> None:
        if (
            prepared_projection.__code__ is not prepared_projection_code
            or request_digest_projection.__code__ is not request_digest_code
            or response_parser.__code__ is not response_parser_code
            or binding_loader.__code__ is not binding_loader_code
            or binding_projection.__code__ is not binding_projection_code
            or book_authority.__code__ is not book_authority_code
            or store_authority_scope.__code__ is not store_scope_code
            or observation_builder.__code__ is not observation_builder_code
            or observation_projection.__code__ is not observation_projection_code
            or digest_function.__code__ is not digest_code
            or book_type.order is not book_order
            or book_order.__code__ is not book_order_code
            or book_type.sync_submission_attempt is not book_sync
            or book_sync.__code__ is not book_sync_code
            or book_type.acknowledge is not book_acknowledge
            or book_acknowledge.__code__ is not book_acknowledge_code
            or artifact_type.publish_bytes is not artifact_publish
            or artifact_publish.__code__ is not artifact_publish_code
        ):
            raise projection_error_type(
                "authenticated Bybit projection executable authority changed"
            )

    def project_authenticated_bybit_submission(
        book: DurableOrderBookProjection,
        *,
        attempt_id: str,
        prepared_request: BybitPreparedSubmission,
    ) -> DurableOrderMutationResult:
        """Resolve one already-sent Bybit attempt without resending.

        Generic send truth is projected first. Therefore any later parser,
        evidence-publication or ACK-commit failure leaves the durable OMS at
        UNKNOWN instead of silently restoring send authority.
        """

        implementation_changed()
        if canonical_type(book) is not book_type:
            raise type_error("book must be exact DurableOrderBookProjection")
        if canonical_type(prepared_request) is not prepared_type:
            raise type_error("prepared_request must be exact BybitPreparedSubmission")

        store, store_identity = book_authority(book)
        state = object_getattribute(book, "__dict__")
        provider_id = state["provider_id"]
        account_id = state["account_id"]
        environment = state["environment"]
        artifact_store = state["evidence_artifact_store"]

        prepared = prepared_projection(prepared_request)
        if provider_id != "BYBIT":
            raise projection_error_type(
                "authenticated Bybit submission requires BYBIT order projection"
            )
        if (
            prepared["account_id"] != account_id
            or prepared["environment"] != environment
        ):
            raise projection_error_type(
                "Bybit prepared request scope differs from durable order projection"
            )

        body = prepared["body"]
        if canonical_type(body) is not canonical_dict:
            raise projection_error_type(
                "Bybit prepared request body projection is non-canonical"
            )
        client_order_id = body.get("orderLinkId")
        if canonical_type(client_order_id) is not canonical_str or not client_order_id:
            raise projection_error_type(
                "Bybit prepared request lacks canonical client order identity"
            )
        book_order(book, client_order_id)

        # Record irreversible send truth before interpreting provider semantics.
        generic_results = book_sync(book, attempt_id=attempt_id)
        if not generic_results:
            raise projection_error_type(
                "submission attempt has no durable outbound send to normalize"
            )
        generic_result = generic_results[-1]

        implementation_changed()
        store_after, store_identity_after = book_authority(book)
        if store_after is not store or store_identity_after != store_identity:
            raise projection_error_type(
                "durable OMS JournalStore authority changed during normalization"
            )
        with store_authority_scope(store, store_identity):
            binding = binding_loader(
                store,
                environment=environment,
                account_id=account_id,
                attempt_id=attempt_id,
            )
        bound = binding_projection(binding)
        if bound["terminal_state"] != "SENT":
            return generic_result

        prepared_request_sha256 = request_digest_projection(prepared_request)
        observation = observation_builder(
            response_binding=binding,
            provider_id="BYBIT",
            endpoint=prepared["endpoint"],
            prepared_request_sha256=prepared_request_sha256,
            capability_snapshot_ids=canonical_tuple(
                prepared["capability_snapshot_ids"]
            ),
            instrument_versions=canonical_tuple(prepared["instrument_versions"]),
        )
        normalized = response_parser(
            attempt_id=attempt_id,
            prepared_request=prepared_request,
            observation=observation,
        )
        if normalized.get("client_order_id") != client_order_id:
            raise projection_error_type(
                "normalized Bybit response changed client order identity"
            )

        outcome = normalized.get("outcome")
        if outcome == "UNKNOWN":
            return generic_result
        if outcome not in {"ACKNOWLEDGED", "REJECTED"}:
            raise projection_error_type(
                "Bybit submission normalizer returned unsupported lifecycle outcome"
            )

        observed = observation_projection(observation)
        evidence = normalized.get("evidence")
        if (
            canonical_type(evidence) is not canonical_list
            or canonical_len(evidence) != 1
            or canonical_type(evidence[0]) is not canonical_dict
        ):
            raise projection_error_type(
                "Bybit normalized lifecycle result lacks one canonical evidence reference"
            )
        evidence_ref = canonical_dict(evidence[0])
        if evidence_ref.get("sha256") != bound["response_sha256"]:
            raise projection_error_type(
                "Bybit normalized evidence digest differs from durable response"
            )
        if evidence_ref.get("observed_at") != observed["sent_at"]:
            raise projection_error_type(
                "Bybit normalized evidence time differs from durable response"
            )

        provider_order_id = normalized.get("provider_order_id")
        acknowledge_request = {
            "client_order_id": client_order_id,
            "provider_order_id": provider_order_id,
            "status": outcome,
            "attempt_id": attempt_id,
        }

        if environment in {"PAPER", "LIVE"} and canonical_type(artifact_store) is not artifact_type:
            raise projection_error_type(
                "authenticated Bybit lifecycle projection requires trusted ArtifactStore"
            )
        if artifact_store is not None:
            if canonical_type(artifact_store) is not artifact_type:
                raise projection_error_type(
                    "provider evidence ArtifactStore authority is non-canonical"
                )
            source_uri = evidence_ref.get("source_uri")
            rights_id = evidence_ref.get("rights_id")
            if canonical_type(source_uri) is not canonical_str or not source_uri:
                raise projection_error_type(
                    "Bybit normalized evidence source is non-canonical"
                )
            if canonical_type(rights_id) is not canonical_str or not rights_id:
                raise projection_error_type(
                    "Bybit normalized evidence rights id is non-canonical"
                )
            implementation_changed()
            manifest = artifact_publish(
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
                    "request_hash": digest_function(acknowledge_request),
                    "observed_at": evidence_ref["observed_at"],
                    "rights_id": rights_id,
                },
            )
            if (
                manifest.get("artifact_id") != evidence_ref["artifact_id"]
                or manifest.get("sha256") != evidence_ref["sha256"]
            ):
                raise projection_error_type(
                    "published Bybit response evidence differs from normalized evidence"
                )

        implementation_changed()
        return book_acknowledge(
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


project_authenticated_bybit_submission = _install_authenticated_bybit_submission_projector(
    book_type=DurableOrderBookProjection,
    prepared_type=BybitPreparedSubmission,
    prepared_projection=guarded_order_projection,
    request_digest_projection=guarded_order_request_sha256,
    response_parser=parse_submission_response,
    binding_loader=load_submission_response_binding,
    binding_projection=submission_response_binding_projection,
    book_authority=require_exact_order_projection_authority,
    store_authority_scope=journal_store_authority_scope,
    observation_builder=observe_submission_json_response,
    observation_projection=provider_submission_observation_projection,
    artifact_type=ArtifactStore,
    digest_function=payload_digest,
)
del _install_authenticated_bybit_submission_projector
