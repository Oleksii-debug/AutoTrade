from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
import uuid


MUTATING_MODES = frozenset({"SOURCE_MUTATION", "INTEGRATION"})
NON_MUTATING_MODES = frozenset({"READ_ONLY_AUDIT", "RESEARCH", "CI_TRIAGE"})
ALLOWED_MODES = MUTATING_MODES | NON_MUTATING_MODES
REGISTRY_MODE_ENABLED = "ATOMIC_CLAIMS_ENABLED"
REGISTRY_MODES_DISABLED = frozenset({
    "BOOTSTRAP_NOT_ENABLED",
    "PROTOCOL_IMPLEMENTED_NOT_ENABLED",
})
REGISTRY_MODES = REGISTRY_MODES_DISABLED | {REGISTRY_MODE_ENABLED}


class RegistryProtocolError(ValueError):
    pass


class RegistryCollisionError(RegistryProtocolError):
    pass


class RegistryStaleGenerationError(RegistryProtocolError):
    pass


def parse_instant(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RegistryProtocolError("instant must be a non-empty ISO-8601 string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        result = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RegistryProtocolError("invalid ISO-8601 instant") from exc
    if result.tzinfo is None:
        raise RegistryProtocolError("instant must include timezone")
    return result.astimezone(timezone.utc)


def format_instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_text(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryProtocolError(f"{key} must be non-empty text")
    canonical = value.strip()
    if canonical != value:
        raise RegistryProtocolError(f"{key} must not contain surrounding whitespace")
    return canonical


def _require_generation(registry: Mapping[str, Any], expected_generation: int) -> int:
    actual = registry.get("generation")
    if type(actual) is not int or actual < 0:
        raise RegistryProtocolError("registry generation must be a nonnegative integer")
    if type(expected_generation) is not int or expected_generation < 0:
        raise RegistryProtocolError("expected_generation must be a nonnegative integer")
    if actual != expected_generation:
        raise RegistryStaleGenerationError(
            f"stale generation: expected {expected_generation}, actual {actual}"
        )
    return actual


def _normalized_scopes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RegistryProtocolError("mutation_scope must be a non-empty list")
    scopes: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise RegistryProtocolError("mutation_scope entries must be non-empty text")
        scope = raw.strip()
        if scope != raw:
            raise RegistryProtocolError("mutation_scope entries must be canonical")
        if scope.startswith("/") or "\\" in scope or ":" in scope or any(c in scope for c in "*?[]\x00\n\r"):
            raise RegistryProtocolError("mutation_scope must be literal repository-relative paths")
        scope = scope.rstrip("/")
        if any(part in {"", ".", ".."} for part in scope.split("/")):
            raise RegistryProtocolError("mutation_scope must not contain empty/dot path segments")
        scopes.append(scope)
    if not scopes:
        raise RegistryProtocolError("mutation_scope must not be empty")
    if len({s.casefold() for s in scopes}) != len(scopes):
        raise RegistryProtocolError("mutation_scope entries must be unique")
    return tuple(sorted(scopes))


def _active(claim: Mapping[str, Any], now: datetime) -> bool:
    if claim.get("status") != "ACTIVE":
        return False
    lease_until = claim.get("lease_until")
    if not isinstance(lease_until, str):
        return False
    try:
        return parse_instant(lease_until) > now
    except RegistryProtocolError:
        return False


def scope_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_scopes = _normalized_scopes(left.get("mutation_scope"))
    right_scopes = _normalized_scopes(right.get("mutation_scope"))
    return any(path_covers(a, b) or path_covers(b, a) for a in left_scopes for b in right_scopes)


def path_covers(parent: str, child: str) -> bool:
    # Conservative on all platforms because the same checkout must work on Windows.
    parent, child = parent.casefold(), child.casefold()
    return child == parent or child.startswith(parent + "/")


def _canonical_request(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise RegistryProtocolError("request must be an object")
    mode = _require_text(request, "claim_mode")
    if mode not in ALLOWED_MODES:
        raise RegistryProtocolError(
            "claim_mode must be one of " + ", ".join(sorted(ALLOWED_MODES))
        )
    claim = {
        "request_id": _require_text(request, "request_id"),
        "run_id": _require_text(request, "run_id"),
        "account_id": _require_text(request, "account_id"),
        "claim_mode": mode,
        "authority_family": _require_text(request, "authority_family"),
        "semantic_key": _require_text(request, "semantic_key"),
        "mutation_scope": list(_normalized_scopes(request.get("mutation_scope"))),
        "lease_until": format_instant(parse_instant(_require_text(request, "lease_until"))),
        "base_head": _require_text(request, "base_head"),
        "contract_versions": request.get("contract_versions", {}),
    }
    if not isinstance(claim["contract_versions"], Mapping):
        raise RegistryProtocolError("contract_versions must be an object")
    claim["contract_versions"] = dict(claim["contract_versions"])
    if not all(
        isinstance(k, str) and k and isinstance(v, str) and v
        for k, v in claim["contract_versions"].items()
    ):
        raise RegistryProtocolError("contract_versions must map non-empty text to non-empty text")
    return claim


def _validate_registry(registry: Mapping[str, Any]) -> None:
    if not isinstance(registry, Mapping):
        raise RegistryProtocolError("registry must be an object")
    if registry.get("schema_version") != "1.0.0":
        raise RegistryProtocolError("unsupported registry schema_version")
    mode = registry.get("mode")
    if mode not in REGISTRY_MODES:
        raise RegistryProtocolError("registry mode is unsupported")
    generation = registry.get("generation")
    if type(generation) is not int or generation < 0:
        raise RegistryProtocolError("registry generation must be a nonnegative integer")
    claims = registry.get("claims")
    if not isinstance(claims, list):
        raise RegistryProtocolError("registry claims must be a list")
    seen_claim_ids: set[str] = set()
    seen_request_ids: set[str] = set()
    for index, claim in enumerate(claims):
        if not isinstance(claim, Mapping):
            raise RegistryProtocolError(f"claims[{index}] must be an object")
        claim_id = claim.get("claim_id")
        request_id = claim.get("request_id")
        if not isinstance(claim_id, str) or not claim_id:
            raise RegistryProtocolError(f"claims[{index}] missing claim_id")
        if not isinstance(request_id, str) or not request_id:
            raise RegistryProtocolError(f"claims[{index}] missing request_id")
        if claim_id in seen_claim_ids:
            raise RegistryProtocolError("duplicate claim_id")
        if request_id in seen_request_ids:
            raise RegistryProtocolError("duplicate request_id")
        seen_claim_ids.add(claim_id)
        seen_request_ids.add(request_id)
        _canonical_request(claim)
        if claim.get("status") not in {"ACTIVE", "EXPIRED", "RELEASED"}:
            raise RegistryProtocolError("invalid claim status")
        cg = claim.get("claim_generation")
        if type(cg) is not int or not 1 <= cg <= generation:
            raise RegistryProtocolError("invalid claim generation")


def expire_leases(registry: Mapping[str, Any], *, now: str) -> dict[str, Any]:
    _validate_registry(registry)
    resolved_now = parse_instant(now)
    result = deepcopy(dict(registry))
    changed = False
    for claim in result["claims"]:
        if claim.get("status") == "ACTIVE":
            lease = claim.get("lease_until")
            try:
                expired = not isinstance(lease, str) or parse_instant(lease) <= resolved_now
            except RegistryProtocolError:
                expired = True
            if expired:
                claim["status"] = "EXPIRED"
                claim["closed_at"] = format_instant(resolved_now)
                changed = True
    if changed:
        result["generation"] += 1
        result["updated_at"] = format_instant(resolved_now)
    return result


def claim(
    registry: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    expected_generation: int,
    now: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _validate_registry(registry)
    resolved_now = parse_instant(now)
    canonical = _canonical_request(request)
    if (
        canonical["claim_mode"] in MUTATING_MODES
        and registry.get("mode") != REGISTRY_MODE_ENABLED
    ):
        raise RegistryProtocolError(
            "registry mutation claims are disabled until atomic ownership is enabled"
        )

    for existing in registry["claims"]:
        if existing.get("request_id") == canonical["request_id"]:
            comparable = existing.get("original_request") or {
                key: existing.get(key)
                for key in (
                    "request_id",
                    "run_id",
                    "account_id",
                    "claim_mode",
                    "authority_family",
                    "semantic_key",
                    "mutation_scope",
                    "lease_until",
                    "base_head",
                    "contract_versions",
                )
            }
            if comparable != canonical:
                raise RegistryProtocolError("request_id reuse with different claim payload")
            return deepcopy(dict(registry)), deepcopy(dict(existing))

    _require_generation(registry, expected_generation)
    if parse_instant(canonical["lease_until"]) <= resolved_now:
        raise RegistryProtocolError("lease_until must be in the future")

    if canonical["claim_mode"] in MUTATING_MODES:
        for existing in registry["claims"]:
            if (
                existing.get("claim_mode") in MUTATING_MODES
                and _active(existing, resolved_now)
                and scope_overlap(existing, canonical)
            ):
                raise RegistryCollisionError(
                    "overlapping live mutation owner: "
                    f"{existing.get('run_id')} / {existing.get('claim_id')}"
                )

    next_registry = deepcopy(dict(registry))
    claim_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "autotrade-claim:" + canonical["request_id"]))
    created = {
        "claim_id": claim_id,
        **canonical,
        "original_request": deepcopy(canonical),
        "status": "ACTIVE",
        "claimed_at": format_instant(resolved_now),
        "claim_generation": expected_generation + 1,
    }
    next_registry["claims"].append(created)
    next_registry["generation"] = expected_generation + 1
    next_registry["updated_at"] = format_instant(resolved_now)
    return next_registry, deepcopy(created)


def renew(
    registry: Mapping[str, Any],
    *,
    claim_id: str,
    run_id: str,
    lease_until: str,
    expected_generation: int,
    now: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _validate_registry(registry)
    _require_generation(registry, expected_generation)
    resolved_now = parse_instant(now)
    new_lease = parse_instant(lease_until)
    if new_lease <= resolved_now:
        raise RegistryProtocolError("renewed lease must be in the future")

    next_registry = deepcopy(dict(registry))
    target = next((c for c in next_registry["claims"] if c.get("claim_id") == claim_id), None)
    if target is None:
        raise RegistryProtocolError("claim_id not found")
    if target.get("run_id") != run_id:
        raise RegistryProtocolError("run_id does not own claim")
    if not _active(target, resolved_now):
        raise RegistryProtocolError("claim is not active")
    if new_lease <= parse_instant(target["lease_until"]):
        raise RegistryProtocolError("renewal must extend lease")

    target["lease_until"] = format_instant(new_lease)
    target["last_renewed_at"] = format_instant(resolved_now)
    target["claim_generation"] = expected_generation + 1
    next_registry["generation"] = expected_generation + 1
    next_registry["updated_at"] = format_instant(resolved_now)
    return next_registry, deepcopy(target)


def release(
    registry: Mapping[str, Any],
    *,
    claim_id: str,
    run_id: str,
    expected_generation: int,
    now: str,
    reason: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _validate_registry(registry)
    _require_generation(registry, expected_generation)
    resolved_now = parse_instant(now)
    if not isinstance(reason, str) or not reason.strip():
        raise RegistryProtocolError("release reason must be non-empty")

    next_registry = deepcopy(dict(registry))
    target = next((c for c in next_registry["claims"] if c.get("claim_id") == claim_id), None)
    if target is None:
        raise RegistryProtocolError("claim_id not found")
    if target.get("run_id") != run_id:
        raise RegistryProtocolError("run_id does not own claim")
    if not _active(target, resolved_now):
        raise RegistryProtocolError("claim is not active")

    target["status"] = "RELEASED"
    target["closed_at"] = format_instant(resolved_now)
    target["release_reason"] = reason.strip()
    target["claim_generation"] = expected_generation + 1
    next_registry["generation"] = expected_generation + 1
    next_registry["updated_at"] = format_instant(resolved_now)
    return next_registry, deepcopy(target)


def active_mutation_claims(registry: Mapping[str, Any], *, now: str) -> list[dict[str, Any]]:
    _validate_registry(registry)
    resolved_now = parse_instant(now)
    return [
        deepcopy(dict(claim))
        for claim in registry["claims"]
        if claim.get("claim_mode") in MUTATING_MODES and _active(claim, resolved_now)
    ]
