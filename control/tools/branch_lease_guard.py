from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


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


def evaluate_guard(
    registry: Mapping[str, Any],
    mutation_state: Mapping[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    """Fail closed unless all observed branch movement belongs to one live owner.

    Adapted from the Autosport branch-lease guard, but ownership authority is
    AutoTrade's dedicated registry branch rather than issue comments.
    """
    if not isinstance(registry, Mapping) or not isinstance(mutation_state, Mapping):
        raise ValueError("registry and mutation_state must be objects")

    semantic_key = mutation_state.get("semantic_key")
    authority_family = mutation_state.get("authority_family")
    mutation_scope = mutation_state.get("mutation_scope")
    branch = mutation_state.get("branch")
    prior_head = mutation_state.get("prior_head")
    current_head = mutation_state.get("current_head")
    for name, value in (
        ("semantic_key", semantic_key),
        ("authority_family", authority_family),
        ("branch", branch),
        ("prior_head", prior_head),
        ("current_head", current_head),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-empty text")
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
        "admitted_owner_run_id": None,
        "mutation_writer_run_ids": [],
        "evidence": [],
    }

    generation = registry.get("generation")
    claims = registry.get("claims")
    if type(generation) is not int or generation < 0 or not isinstance(claims, list):
        return {**base, "status": "AMBIGUOUS", "evidence": ["malformed registry"]}

    now_dt = _parse_instant(now)
    scope_set = set(mutation_scope)
    owners: list[Mapping[str, Any]] = []
    for claim in claims:
        if not isinstance(claim, Mapping):
            return {**base, "status": "AMBIGUOUS", "evidence": ["malformed claim"]}
        if claim.get("status") != "ACTIVE":
            continue
        if claim.get("claim_mode") not in {"SOURCE_MUTATION", "INTEGRATION"}:
            continue
        if claim.get("semantic_key") != semantic_key or claim.get("authority_family") != authority_family:
            continue
        claim_scopes = claim.get("mutation_scope")
        if not isinstance(claim_scopes, list):
            return {**base, "status": "AMBIGUOUS", "evidence": ["malformed claim scope"]}
        if not scope_set.intersection(claim_scopes):
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
    owner_run_id = owner.get("run_id")
    if not isinstance(owner_run_id, str) or not owner_run_id:
        return {**base, "status": "AMBIGUOUS", "evidence": ["owner missing run_id"]}
    base["admitted_owner_run_id"] = owner_run_id

    if prior_head == current_head:
        return {**base, "status": "OK", "evidence": ["branch did not move"]}

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
    return {**base, "status": "OK", "evidence": [f"complete branch movement attributed to {owner_run_id}"]}
