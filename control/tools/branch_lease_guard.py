from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from control.tools.registry_state import (
    REGISTRY_MODE_ENABLED,
    _validate_registry,
    _normalized_scopes,
    path_covers,
    scope_overlap,
)


STATUSES = frozenset({"OK", "COLLISION", "AMBIGUOUS", "NO_LIVE_OWNER"})


def _parse_instant(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("instant must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    result = datetime.fromisoformat(text)
    if result.tzinfo is None:
        raise ValueError("instant must include timezone")
    return result.astimezone(timezone.utc)


def _records(value: Any, key: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    if not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"{key} entries must be objects")
    return list(value)


def _positive_generation(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def evaluate_guard(
    registry: Mapping[str, Any],
    mutation_state: Mapping[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    """Fail closed unless branch movement belongs to one exact live claim fence.

    Ownership is valid only while the registry is in its explicitly enabled mode.
    A branch movement is bound to the live claim ID *and* claim generation, not
    merely to the reusable run ID. Every mutation record carries the same fence,
    so evidence from an older claim generation cannot be relabelled at the top
    level after a renewal or handoff transition. Once review has been submitted,
    the reviewed branch is frozen; a changed head requires a new claim/review
    lineage rather than mutating underneath an exact-head authorization.
    """
    if not isinstance(registry, Mapping) or not isinstance(mutation_state, Mapping):
        raise ValueError("registry and mutation_state must be objects")

    semantic_key = mutation_state.get("semantic_key")
    authority_family = mutation_state.get("authority_family")
    mutation_scope = mutation_state.get("mutation_scope")
    branch = mutation_state.get("branch")
    prior_head = mutation_state.get("prior_head")
    current_head = mutation_state.get("current_head")
    claim_id = mutation_state.get("claim_id")
    claim_generation = mutation_state.get("claim_generation")
    for name, value in (
        ("semantic_key", semantic_key),
        ("authority_family", authority_family),
        ("branch", branch),
        ("prior_head", prior_head),
        ("current_head", current_head),
        ("claim_id", claim_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-empty text")
    try:
        requested_claim_generation = _positive_generation(
            claim_generation,
            name="claim_generation",
        )
    except ValueError as exc:
        return {
            "semantic_key": semantic_key,
            "authority_family": authority_family,
            "mutation_scope": mutation_scope,
            "branch": branch,
            "prior_head": prior_head,
            "current_head": current_head,
            "branch_moved": prior_head != current_head,
            "requested_claim_id": claim_id,
            "requested_claim_generation": claim_generation,
            "admitted_owner_run_id": None,
            "admitted_claim_id": None,
            "admitted_claim_generation": None,
            "mutation_writer_run_ids": [],
            "status": "AMBIGUOUS",
            "evidence": [str(exc)],
        }
    if not isinstance(mutation_scope, list) or not mutation_scope:
        raise ValueError("mutation_scope must be a non-empty list")

    base = {
        "semantic_key": semantic_key,
        "authority_family": authority_family,
        "mutation_scope": mutation_scope,
        "branch": branch,
        "prior_head": prior_head,
        "current_head": current_head,
        "branch_moved": prior_head != current_head,
        "requested_claim_id": claim_id,
        "requested_claim_generation": requested_claim_generation,
        "admitted_owner_run_id": None,
        "admitted_claim_id": None,
        "admitted_claim_generation": None,
        "mutation_writer_run_ids": [],
        "evidence": [],
    }

    try:
        _validate_registry(registry)
        mutation_scope = _normalized_scopes(mutation_scope)
    except ValueError as exc:
        return {**base, "status": "AMBIGUOUS", "evidence": [str(exc)]}

    if registry.get("mode") != REGISTRY_MODE_ENABLED:
        return {
            **base,
            "status": "NO_LIVE_OWNER",
            "evidence": ["registry mutation ownership is disabled"],
        }

    generation = registry.get("generation")
    claims = registry.get("claims")
    if type(generation) is not int or generation < 0 or not isinstance(claims, list):
        return {**base, "status": "AMBIGUOUS", "evidence": ["malformed registry"]}

    now_dt = _parse_instant(now)
    owners: list[Mapping[str, Any]] = []
    for claim in claims:
        if not isinstance(claim, Mapping):
            return {**base, "status": "AMBIGUOUS", "evidence": ["malformed claim"]}
        if claim.get("status") != "ACTIVE":
            continue
        if claim.get("claim_mode") not in {"SOURCE_MUTATION", "INTEGRATION"}:
            continue
        claim_scopes = claim.get("mutation_scope")
        if not isinstance(claim_scopes, list):
            return {**base, "status": "AMBIGUOUS", "evidence": ["malformed claim scope"]}
        if not scope_overlap(claim, mutation_state):
            continue
        lease_until = claim.get("lease_until")
        try:
            live = isinstance(lease_until, str) and _parse_instant(lease_until) > now_dt
        except ValueError:
            return {**base, "status": "AMBIGUOUS", "evidence": ["malformed lease"]}
        if live:
            owners.append(claim)

    if len(owners) > 1:
        return {**base, "status": "AMBIGUOUS", "evidence": ["multiple overlapping live mutation owners"]}
    if not owners:
        return {**base, "status": "NO_LIVE_OWNER", "evidence": ["no overlapping live mutation owner"]}

    owner = owners[0]
    if owner.get("semantic_key") != semantic_key or owner.get("authority_family") != authority_family:
        return {**base, "status": "COLLISION", "evidence": ["path belongs to another semantic owner"]}
    if not all(
        any(path_covers(a, b) for a in _normalized_scopes(owner["mutation_scope"]))
        for b in mutation_scope
    ):
        return {**base, "status": "AMBIGUOUS", "evidence": ["owner does not cover every changed path"]}

    owner_run_id = owner.get("run_id")
    owner_claim_id = owner.get("claim_id")
    owner_claim_generation = owner.get("claim_generation")
    if not isinstance(owner_run_id, str) or not owner_run_id:
        return {**base, "status": "AMBIGUOUS", "evidence": ["owner missing run_id"]}
    if not isinstance(owner_claim_id, str) or not owner_claim_id:
        return {**base, "status": "AMBIGUOUS", "evidence": ["owner missing claim_id"]}
    if type(owner_claim_generation) is not int or owner_claim_generation < 1:
        return {**base, "status": "AMBIGUOUS", "evidence": ["owner missing claim_generation"]}

    base["admitted_owner_run_id"] = owner_run_id
    base["admitted_claim_id"] = owner_claim_id
    base["admitted_claim_generation"] = owner_claim_generation
    if claim_id != owner_claim_id:
        return {
            **base,
            "status": "COLLISION",
            "evidence": ["mutation fence claim_id does not match live owner"],
        }
    if requested_claim_generation != owner_claim_generation:
        return {
            **base,
            "status": "COLLISION",
            "evidence": ["mutation fence uses stale or foreign claim_generation"],
        }

    if prior_head == current_head:
        return {**base, "status": "OK", "evidence": ["branch did not move"]}

    if owner.get("handoff_state", "CLAIMED") != "CLAIMED":
        return {
            **base,
            "status": "COLLISION",
            "evidence": ["branch movement is frozen after exact-head review submission"],
        }

    try:
        mutations = _records(mutation_state.get("mutations"), "mutations")
    except ValueError as exc:
        return {**base, "status": "AMBIGUOUS", "evidence": [str(exc)]}
    if not mutations:
        return {**base, "status": "AMBIGUOUS", "evidence": ["branch moved without mutation evidence"]}

    writers: list[str] = []
    seen_heads: set[str] = set()
    expected_parent = prior_head
    for index, mutation in enumerate(mutations):
        head = mutation.get("head")
        run_id = mutation.get("run_id")
        record_claim_id = mutation.get("claim_id")
        record_claim_generation = mutation.get("claim_generation")
        parents = mutation.get("parent_heads")
        if not isinstance(head, str) or not head.strip():
            return {**base, "status": "AMBIGUOUS", "mutation_writer_run_ids": writers, "evidence": [f"mutations[{index}] missing head"]}
        head = head.strip()
        if head in seen_heads:
            return {**base, "status": "AMBIGUOUS", "mutation_writer_run_ids": writers, "evidence": [f"duplicate mutation head {head}"]}
        seen_heads.add(head)
        if not isinstance(parents, list) or not parents or not all(isinstance(p, str) and p.strip() for p in parents):
            return {**base, "status": "AMBIGUOUS", "mutation_writer_run_ids": writers, "evidence": [f"mutations[{index}] missing parent_heads"]}
        normalized_parents = [p.strip() for p in parents]
        if len(set(normalized_parents)) != len(normalized_parents) or head in normalized_parents:
            return {**base, "status": "AMBIGUOUS", "mutation_writer_run_ids": writers, "evidence": [f"mutations[{index}] invalid parent set"]}
        if expected_parent not in normalized_parents:
            return {**base, "status": "AMBIGUOUS", "mutation_writer_run_ids": writers, "evidence": [f"non-contiguous mutation evidence at {index}"]}
        if not isinstance(run_id, str) or not run_id.strip():
            return {**base, "status": "AMBIGUOUS", "mutation_writer_run_ids": writers, "evidence": [f"mutations[{index}] missing run_id"]}
        if not isinstance(record_claim_id, str) or not record_claim_id.strip():
            return {**base, "status": "AMBIGUOUS", "mutation_writer_run_ids": writers, "evidence": [f"mutations[{index}] missing claim_id"]}
        try:
            normalized_record_generation = _positive_generation(
                record_claim_generation,
                name=f"mutations[{index}].claim_generation",
            )
        except ValueError as exc:
            return {**base, "status": "AMBIGUOUS", "mutation_writer_run_ids": writers, "evidence": [str(exc)]}
        if record_claim_id.strip() != owner_claim_id or normalized_record_generation != owner_claim_generation:
            return {
                **base,
                "status": "COLLISION",
                "mutation_writer_run_ids": writers,
                "evidence": [f"mutations[{index}] is fenced to a stale or foreign claim"],
            }
        writers.append(run_id.strip())
        expected_parent = head

    if expected_parent != current_head:
        return {**base, "status": "AMBIGUOUS", "mutation_writer_run_ids": writers, "evidence": ["mutation evidence not bound to current_head"]}

    base["mutation_writer_run_ids"] = writers
    foreign = sorted({writer for writer in writers if writer != owner_run_id})
    if foreign:
        return {
            **base,
            "status": "COLLISION",
            "evidence": [f"live owner {owner_run_id}; foreign writer(s): " + ", ".join(foreign)],
        }
    return {
        **base,
        "status": "OK",
        "evidence": [
            f"complete branch movement attributed to {owner_run_id} / {owner_claim_id} generation {owner_claim_generation}"
        ],
    }
