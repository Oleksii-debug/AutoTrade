from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import tarfile


SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
ANNOTATION_KEYS = frozenset({"title", "description", "$comment", "examples"})
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head", "trace"})


def parse_version(value: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(value)
    if not match:
        raise ValueError(f"Invalid semantic version: {value!r}")
    return tuple(int(part) for part in match.groups())


def load_manifest(root: Path) -> dict:
    return json.loads((root / "contracts" / "manifest.json").read_text(encoding="utf-8"))


def schema_definitions(root: Path, names: list[str]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for name in names:
        path = root / "contracts" / "jsonschema" / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        defs = payload.get("$defs", {})
        if not isinstance(defs, dict):
            raise ValueError(f"{name} has invalid $defs")
        result[name] = defs
    return result


def schema_required_members(
    root: Path,
    names: list[str],
) -> dict[str, dict[str, set[str]]]:
    """Return required-member sets keyed by stable JSON-tree path.

    Only paths present in both contract revisions are compared. A newly added
    definition/object may introduce its own required fields without breaking
    existing instances, while adding a required member to an existing object
    makes previously valid payloads invalid and therefore requires a major bump.
    """

    result: dict[str, dict[str, set[str]]] = {}
    for name in names:
        payload = json.loads(
            (root / "contracts" / "jsonschema" / name).read_text(encoding="utf-8")
        )
        paths: dict[str, set[str]] = {}

        def visit(value: object, path: str) -> None:
            if isinstance(value, dict):
                required = value.get("required", [])
                if (
                    not isinstance(required, list)
                    or any(not isinstance(item, str) or not item for item in required)
                ):
                    raise ValueError(f"{name} has invalid required array at {path}")
                # Record the object path even when required is absent. This
                # compares an existing empty required-set with the same object
                # after a member becomes required. Truly new paths remain
                # excluded by the base/current path intersection below.
                paths[path] = set(required)
                for key, child in value.items():
                    visit(child, f"{path}/{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, f"{path}/{index}")

        visit(payload, "$")
        result[name] = paths
    return result


def _semantic_shape(value: object, *, schema_base_uri: str) -> object:
    """Normalize version-only local references and ignore documentation annotations."""
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        for key, child in value.items():
            if key in ANNOTATION_KEYS:
                continue
            normalized[key] = _semantic_shape(child, schema_base_uri=schema_base_uri)
        return normalized
    if isinstance(value, list):
        return [_semantic_shape(child, schema_base_uri=schema_base_uri) for child in value]
    if isinstance(value, str) and schema_base_uri and value.startswith(schema_base_uri):
        return "{SCHEMA_BASE}/" + value[len(schema_base_uri):].lstrip("/")
    return value


def changed_existing_definitions(
    base_root: Path,
    current_root: Path,
    schema_names: list[str],
    *,
    base_uri: str,
    current_uri: str,
) -> dict[str, list[str]]:
    base_defs = schema_definitions(base_root, schema_names)
    current_defs = schema_definitions(current_root, schema_names)
    changed: dict[str, list[str]] = {}
    for schema_name in schema_names:
        common_defs = sorted(set(base_defs[schema_name]) & set(current_defs[schema_name]))
        names = [
            name
            for name in common_defs
            if _semantic_shape(base_defs[schema_name][name], schema_base_uri=base_uri)
            != _semantic_shape(current_defs[schema_name][name], schema_base_uri=current_uri)
        ]
        if names:
            changed[schema_name] = names
    return changed


def openapi_operation_blocks(
    root: Path,
    manifest: dict,
    *,
    schema_base_uri: str,
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Extract the reviewed OpenAPI operation surface without adding a YAML dependency."""
    openapi = manifest.get("openapi")
    if not isinstance(openapi, dict):
        return {}
    relative = openapi.get("path")
    if not isinstance(relative, str) or not relative:
        raise ValueError("manifest openapi.path must be non-empty text")
    lines = (root / relative).read_text(encoding="utf-8").splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line == "paths:")
    except StopIteration as error:
        raise ValueError("OpenAPI document must contain top-level paths") from error

    operations: dict[tuple[str, str], list[str]] = {}
    current_path: str | None = None
    current_key: tuple[str, str] | None = None
    for line in lines[start + 1 :]:
        if line and not line.startswith(" ") and not line.lstrip().startswith("#"):
            break
        path_match = re.fullmatch(r"  (/[^:]+):\s*", line)
        if path_match:
            current_path = path_match.group(1)
            current_key = None
            continue
        method_match = re.fullmatch(
            r"    (" + "|".join(sorted(HTTP_METHODS)) + r"):\s*",
            line,
        )
        if method_match:
            if current_path is None:
                raise ValueError("OpenAPI method appeared before a path")
            current_key = (current_path, method_match.group(1))
            if current_key in operations:
                raise ValueError(f"duplicate OpenAPI operation: {current_key}")
            operations[current_key] = []
            continue
        if current_key is None:
            continue
        if line.strip() == "" or line.lstrip().startswith("#"):
            continue
        if len(line) - len(line.lstrip(" ")) < 6:
            current_key = None
            continue
        normalized = line.strip()
        if normalized.startswith(("description:", "summary:")):
            continue
        if schema_base_uri:
            normalized = normalized.replace(schema_base_uri.rstrip("/") + "/", "{SCHEMA_BASE}/")
        operations[current_key].append(normalized)

    if not operations:
        raise ValueError("OpenAPI document must expose at least one operation")
    return {key: tuple(value) for key, value in operations.items()}


def openapi_security_surface(
    root: Path,
    manifest: dict,
    *,
    schema_base_uri: str,
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """Return canonical global auth requirements and named security schemes.

    OpenAPI top-level security is an unordered list of alternative Security
    Requirement Objects. Keys inside each requirement are AND-ed, so their
    mapping order is also non-semantic. Security-scheme objects are mappings:
    mapping-property order is ignored recursively while sequence order remains
    represented explicitly. This remains a narrow surface parser rather than a
    second general YAML authority.
    """

    openapi = manifest.get("openapi")
    if not isinstance(openapi, dict):
        return (), {}
    relative = openapi.get("path")
    if not isinstance(relative, str) or not relative:
        raise ValueError("manifest openapi.path must be non-empty text")
    lines = (root / relative).read_text(encoding="utf-8").splitlines()

    def normalize_scalar(value: str) -> str:
        normalized = value.strip()
        if schema_base_uri:
            normalized = normalized.replace(
                schema_base_uri.rstrip("/") + "/",
                "{SCHEMA_BASE}/",
            )
        return normalized

    def normalize_scope(value: str) -> str:
        value = normalize_scalar(value)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            return value[1:-1]
        return value

    def inline_scopes(raw: str) -> list[str] | None:
        value = raw.strip()
        if not value:
            return None
        if value == "[]":
            return []
        if not (value.startswith("[") and value.endswith("]")):
            raise ValueError(
                "OpenAPI security requirement scopes must be an array"
            )
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [normalize_scope(item) for item in inner.split(",")]

    default_security: tuple[str, ...] = ()
    for index, line in enumerate(lines):
        match = re.fullmatch(r"security:\s*(.*)", line)
        if match is None:
            continue
        inline = match.group(1).strip()
        if inline:
            # Missing top-level security and explicit security: [] are both
            # unauthenticated defaults under OpenAPI semantics.
            if inline == "[]":
                default_security = ()
            else:
                raise ValueError(
                    "inline top-level OpenAPI security must be [] or use block form"
                )
            break

        requirements: list[str] = []
        current: list[tuple[str, list[str] | None]] | None = None
        current_scope_index: int | None = None

        def finish_requirement() -> None:
            nonlocal current, current_scope_index
            if current is None:
                return
            canonical: list[tuple[str, tuple[str, ...]]] = []
            seen: set[str] = set()
            for scheme, scopes in current:
                if scheme in seen:
                    raise ValueError(
                        f"duplicate scheme in OpenAPI security requirement: {scheme}"
                    )
                seen.add(scheme)
                if scopes is None:
                    raise ValueError(
                        f"OpenAPI security requirement {scheme} has no scope array"
                    )
                canonical.append((scheme, tuple(sorted(scopes))))
            requirements.append(
                json.dumps(
                    sorted(canonical),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            current = None
            current_scope_index = None

        for nested in lines[index + 1 :]:
            if nested and not nested.startswith(" ") and not nested.lstrip().startswith("#"):
                break
            if not nested.strip() or nested.lstrip().startswith("#"):
                continue
            first = re.fullmatch(
                r"  - ([A-Za-z0-9_.-]+):\s*(.*)",
                nested,
            )
            if first:
                finish_requirement()
                current = [(first.group(1), inline_scopes(first.group(2)))]
                current_scope_index = 0
                continue
            additional = re.fullmatch(
                r"    ([A-Za-z0-9_.-]+):\s*(.*)",
                nested,
            )
            if additional:
                if current is None:
                    raise ValueError(
                        "OpenAPI security requirement member appeared before a requirement"
                    )
                current.append(
                    (additional.group(1), inline_scopes(additional.group(2)))
                )
                current_scope_index = len(current) - 1
                continue
            scope_item = re.fullmatch(r"      -\s+(.+?)\s*", nested)
            if scope_item:
                if current is None or current_scope_index is None:
                    raise ValueError(
                        "OpenAPI security scope appeared before a scheme"
                    )
                scheme, scopes = current[current_scope_index]
                if scopes is not None:
                    raise ValueError(
                        f"OpenAPI security requirement {scheme} mixes inline and block scopes"
                    )
                scopes = []
                current[current_scope_index] = (scheme, scopes)
                scopes.append(normalize_scope(scope_item.group(1)))
                continue
            raise ValueError(
                f"unsupported OpenAPI top-level security syntax: {nested.strip()}"
            )
        finish_requirement()
        default_security = tuple(sorted(requirements))
        break

    try:
        components_index = next(
            index for index, line in enumerate(lines) if line == "components:"
        )
    except StopIteration:
        return default_security, {}

    security_index: int | None = None
    for index in range(components_index + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith(" ") and not line.lstrip().startswith("#"):
            break
        if re.fullmatch(r"  securitySchemes:\s*", line):
            security_index = index
            break
    if security_index is None:
        return default_security, {}

    schemes: dict[str, list[str]] = {}
    current_scheme: str | None = None
    for line in lines[security_index + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= 2:
            break
        scheme_match = re.fullmatch(r"    ([A-Za-z0-9_.-]+):\s*", line)
        if scheme_match:
            current_scheme = scheme_match.group(1)
            if current_scheme in schemes:
                raise ValueError(
                    f"duplicate OpenAPI security scheme: {current_scheme}"
                )
            schemes[current_scheme] = []
            continue
        if current_scheme is None:
            raise ValueError("OpenAPI security scheme content appeared before a scheme")
        if indent < 6:
            raise ValueError("invalid OpenAPI security scheme indentation")
        schemes[current_scheme].append(line)

    def canonicalize_scheme(block: list[str]) -> tuple[str, ...]:
        records: list[str] = []
        stack: list[tuple[int, str]] = []
        sequence_counts: dict[tuple[tuple[str, ...], int], int] = {}
        skip_annotation_indent: int | None = None

        for raw_line in block:
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            stripped = raw_line.strip()

            if skip_annotation_indent is not None:
                if indent > skip_annotation_indent:
                    continue
                skip_annotation_indent = None

            while stack and stack[-1][0] >= indent:
                stack.pop()
            parent = tuple(key for _, key in stack)

            if stripped.startswith("- "):
                sequence_key = (parent, indent)
                sequence_index = sequence_counts.get(sequence_key, 0)
                sequence_counts[sequence_key] = sequence_index + 1
                records.append(
                    json.dumps(
                        [
                            "sequence",
                            list(parent),
                            sequence_index,
                            normalize_scalar(stripped[2:]),
                        ],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                continue

            mapping = re.fullmatch(r"([A-Za-z0-9_.$-]+):\s*(.*)", stripped)
            if mapping is None:
                records.append(
                    json.dumps(
                        ["scalar", list(parent), indent, normalize_scalar(stripped)],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                continue

            key, value = mapping.groups()
            if key in {"description", "summary"}:
                if not value or value.startswith(("|", ">")):
                    skip_annotation_indent = indent
                continue

            path = [*parent, key]
            if value:
                records.append(
                    json.dumps(
                        ["mapping", path, normalize_scalar(value)],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            else:
                records.append(
                    json.dumps(
                        ["container", path],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                stack.append((indent, key))

        return tuple(sorted(records))

    return default_security, {
        name: canonicalize_scheme(values)
        for name, values in schemes.items()
    }


def contract_bytes(root: Path) -> dict[str, bytes]:
    base = root / "contracts"
    files: dict[str, bytes] = {}
    for path in sorted(base.rglob("*")):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def evaluate(base_root: Path, current_root: Path) -> list[str]:
    base = load_manifest(base_root)
    current = load_manifest(current_root)
    base_version = parse_version(base["contract_version"])
    current_version = parse_version(current["contract_version"])

    errors: list[str] = []
    if current_version < base_version:
        errors.append("contract_version must never decrease")

    changed = contract_bytes(base_root) != contract_bytes(current_root)
    if changed and current_version == base_version:
        errors.append("contract surface changed without increasing contract_version")

    base_schemas = list(base.get("schemas", []))
    current_schemas = list(current.get("schemas", []))
    removed_schemas = sorted(set(base_schemas) - set(current_schemas))

    common = sorted(set(base_schemas) & set(current_schemas))
    base_defs = schema_definitions(base_root, common)
    current_defs = schema_definitions(current_root, common)
    removed_defs = {
        name: sorted(set(base_defs[name]) - set(current_defs[name]))
        for name in common
        if set(base_defs[name]) - set(current_defs[name])
    }

    base_required = schema_required_members(base_root, common)
    current_required = schema_required_members(current_root, common)
    added_required: dict[str, dict[str, list[str]]] = {}
    for name in common:
        common_paths = set(base_required[name]) & set(current_required[name])
        additions = {
            path: sorted(current_required[name][path] - base_required[name][path])
            for path in sorted(common_paths)
            if current_required[name][path] - base_required[name][path]
        }
        if additions:
            added_required[name] = additions

    changed_defs = changed_existing_definitions(
        base_root,
        current_root,
        common,
        base_uri=str(base.get("schema_base_uri", "")),
        current_uri=str(current.get("schema_base_uri", "")),
    )

    base_operations = openapi_operation_blocks(
        base_root,
        base,
        schema_base_uri=str(base.get("schema_base_uri", "")),
    )
    current_operations = openapi_operation_blocks(
        current_root,
        current,
        schema_base_uri=str(current.get("schema_base_uri", "")),
    )
    removed_operations = sorted(set(base_operations) - set(current_operations))
    changed_operations = sorted(
        key
        for key in set(base_operations) & set(current_operations)
        if base_operations[key] != current_operations[key]
    )

    base_default_security, base_security_schemes = openapi_security_surface(
        base_root,
        base,
        schema_base_uri=str(base.get("schema_base_uri", "")),
    )
    current_default_security, current_security_schemes = openapi_security_surface(
        current_root,
        current,
        schema_base_uri=str(current.get("schema_base_uri", "")),
    )
    changed_default_security = base_default_security != current_default_security
    removed_security_schemes = sorted(
        set(base_security_schemes) - set(current_security_schemes)
    )
    changed_security_schemes = sorted(
        name
        for name in set(base_security_schemes) & set(current_security_schemes)
        if base_security_schemes[name] != current_security_schemes[name]
    )

    breaking_change = bool(
        removed_schemas
        or removed_defs
        or added_required
        or changed_defs
        or removed_operations
        or changed_operations
        or changed_default_security
        or removed_security_schemes
        or changed_security_schemes
    )
    if breaking_change and current_version[0] <= base_version[0]:
        details = []
        if removed_schemas:
            details.append("removed schemas: " + ", ".join(removed_schemas))
        for name, defs in removed_defs.items():
            details.append(f"removed definitions from {name}: " + ", ".join(defs))
        for name, paths in added_required.items():
            for path, members in paths.items():
                details.append(
                    f"new required members in {name} at {path}: " + ", ".join(members)
                )
        for name, defs in sorted(changed_defs.items()):
            details.append(f"changed existing definitions in {name}: " + ", ".join(defs))
        if removed_operations:
            details.append(
                "removed OpenAPI operations: "
                + ", ".join(f"{method.upper()} {path}" for path, method in removed_operations)
            )
        if changed_operations:
            details.append(
                "changed OpenAPI operations: "
                + ", ".join(f"{method.upper()} {path}" for path, method in changed_operations)
            )
        if changed_default_security:
            details.append("changed OpenAPI default security requirements")
        if removed_security_schemes:
            details.append(
                "removed OpenAPI security schemes: "
                + ", ".join(removed_security_schemes)
            )
        if changed_security_schemes:
            details.append(
                "changed OpenAPI security schemes: "
                + ", ".join(changed_security_schemes)
            )
        errors.append("breaking contract change requires a new major version; " + "; ".join(details))

    return errors


def export_ref(ref: str) -> Path:
    temporary = Path(tempfile.mkdtemp(prefix="autotrade-contract-base-"))
    archive = subprocess.run(
        ["git", "archive", ref],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        bundle.extractall(temporary, filter="data")
    return temporary


def evaluate_refs(base_ref: str, current_ref: str = "HEAD") -> list[str]:
    """Compare committed contract trees, never test-mutated working-tree bytes."""
    base = export_ref(base_ref)
    current = export_ref(current_ref)
    return evaluate(base, current)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--current-ref", default="HEAD")
    args = parser.parse_args()
    errors = evaluate_refs(args.base_ref, args.current_ref)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Contract version guard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
