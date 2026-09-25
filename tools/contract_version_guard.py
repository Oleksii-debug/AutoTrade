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
) -> tuple[tuple[object, ...], dict[str, object]]:
    """Return canonical global auth requirements and named security schemes.

    OpenAPI security requirement alternatives are unordered, scheme keys inside
    one requirement are conjunctive and therefore unordered, and YAML mapping
    property order is not semantic.  This deliberately small parser canonicalizes
    only the reviewed security subset without introducing a second YAML/version
    authority. Sequence order inside scheme definitions is preserved.
    """

    openapi = manifest.get("openapi")
    if not isinstance(openapi, dict):
        return (), {}
    relative = openapi.get("path")
    if not isinstance(relative, str) or not relative:
        raise ValueError("manifest openapi.path must be non-empty text")
    lines = (root / relative).read_text(encoding="utf-8").splitlines()

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def normalize_scalar(value: str) -> str:
        normalized = value.strip()
        if schema_base_uri:
            normalized = normalized.replace(
                schema_base_uri.rstrip("/") + "/",
                "{SCHEMA_BASE}/",
            )
        return normalized

    def child_indent(block: list[str]) -> int:
        meaningful = [
            indent_of(line)
            for line in block
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not meaningful:
            raise ValueError("OpenAPI security block has no semantic content")
        return min(meaningful)

    def canonical_yaml_block(block: list[str], indent: int) -> object:
        """Canonicalize the YAML subset used by security-scheme definitions."""
        meaningful = [
            line
            for line in block
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not meaningful:
            return ()

        groups: list[list[str]] = []
        current: list[str] = []
        for line in meaningful:
            line_indent = indent_of(line)
            if line_indent < indent:
                raise ValueError("invalid OpenAPI security scheme indentation")
            if line_indent == indent:
                if current:
                    groups.append(current)
                current = [line]
            else:
                if not current:
                    raise ValueError("OpenAPI security child appeared before its parent")
                current.append(line)
        if current:
            groups.append(current)

        list_mode = groups[0][0].strip().startswith("- ")
        if any(group[0][0:indent] != " " * indent for group in groups):
            raise ValueError("invalid OpenAPI security indentation")
        if any(group[0].strip().startswith("- ") != list_mode for group in groups):
            raise ValueError("mixed mapping/list OpenAPI security block")

        if list_mode:
            values: list[object] = []
            for group in groups:
                payload = group[0].strip()[2:].strip()
                children = group[1:]
                mapping_item = re.fullmatch(
                    r"([A-Za-z0-9_.-]+):(?:\s+(.*))?",
                    payload,
                )
                if mapping_item is not None:
                    pseudo = " " * (indent + 2) + payload
                    values.append(
                        canonical_yaml_block([pseudo, *children], indent + 2)
                    )
                    continue
                child = (
                    canonical_yaml_block(children, child_indent(children))
                    if children
                    else ()
                )
                values.append(("scalar", normalize_scalar(payload), child))
            return ("list", tuple(values))

        entries: list[tuple[str, object]] = []
        for group in groups:
            payload = group[0].strip()
            match = re.fullmatch(r"([A-Za-z0-9_.$-]+):\s*(.*)", payload)
            if match is None:
                raise ValueError(f"invalid OpenAPI security mapping entry: {payload!r}")
            key, inline = match.group(1), match.group(2).strip()
            if key in {"description", "summary"}:
                continue
            children = group[1:]
            if inline and children:
                value: object = (
                    "scalar+children",
                    normalize_scalar(inline),
                    canonical_yaml_block(children, child_indent(children)),
                )
            elif inline:
                value = ("scalar", normalize_scalar(inline))
            elif children:
                value = canonical_yaml_block(children, child_indent(children))
            else:
                value = ()
            entries.append((key, value))
        return ("map", tuple(sorted(entries, key=lambda item: item[0])))

    def parse_inline_scopes(value: str) -> tuple[str, ...] | None:
        value = value.strip()
        if not value:
            return None
        if value == "[]":
            return ()
        if not (value.startswith("[") and value.endswith("]")):
            raise ValueError("OpenAPI security requirement scopes must be an array")
        body = value[1:-1].strip()
        if not body:
            return ()
        scopes = [
            normalize_scalar(item.strip().strip("'\""))
            for item in body.split(",")
            if item.strip()
        ]
        return tuple(sorted(scopes))

    def parse_requirement_entry(payload: str) -> tuple[str, tuple[str, ...] | None]:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+):\s*(.*)", payload)
        if match is None:
            raise ValueError(f"invalid OpenAPI security requirement entry: {payload!r}")
        return match.group(1), parse_inline_scopes(match.group(2))

    default_security: tuple[object, ...] = ()
    for index, line in enumerate(lines):
        match = re.fullmatch(r"security:\s*(.*)", line)
        if match is None:
            continue
        inline = match.group(1).strip()
        if inline:
            # Missing top-level security and explicit security: [] both mean an
            # unauthenticated default. Other inline forms are retained exactly.
            default_security = () if inline == "[]" else (("inline", normalize_scalar(inline)),)
            break

        requirements: list[tuple[tuple[str, tuple[str, ...]], ...]] = []
        current_requirement: list[tuple[str, tuple[str, ...]]] = []
        current_scheme: str | None = None
        current_scopes: list[str] = []

        def flush_scheme() -> None:
            nonlocal current_scheme, current_scopes
            if current_scheme is None:
                return
            if any(name == current_scheme for name, _ in current_requirement):
                raise ValueError(
                    f"duplicate OpenAPI security requirement scheme: {current_scheme}"
                )
            current_requirement.append((current_scheme, tuple(sorted(current_scopes))))
            current_scheme = None
            current_scopes = []

        def flush_requirement() -> None:
            flush_scheme()
            if not current_requirement:
                return
            requirements.append(tuple(sorted(current_requirement, key=lambda item: item[0])))
            current_requirement.clear()

        for nested in lines[index + 1 :]:
            if not nested.strip() or nested.lstrip().startswith("#"):
                continue
            nested_indent = indent_of(nested)
            if nested_indent == 0:
                break
            stripped = nested.strip()
            if nested_indent == 2 and stripped.startswith("- "):
                flush_requirement()
                current_scheme, inline_scopes = parse_requirement_entry(stripped[2:].strip())
                current_scopes = [] if inline_scopes is None else list(inline_scopes)
                continue
            if nested_indent == 4 and not stripped.startswith("- "):
                flush_scheme()
                current_scheme, inline_scopes = parse_requirement_entry(stripped)
                current_scopes = [] if inline_scopes is None else list(inline_scopes)
                continue
            if (
                current_scheme is not None
                and nested_indent >= 4
                and stripped.startswith("- ")
            ):
                current_scopes.append(
                    normalize_scalar(stripped[2:].strip().strip("'\""))
                )
                continue
            raise ValueError("unsupported OpenAPI top-level security requirement shape")
        flush_requirement()
        default_security = tuple(sorted(requirements, key=repr))
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

    scheme_blocks: dict[str, list[str]] = {}
    current_scheme: str | None = None
    for line in lines[security_index + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = indent_of(line)
        if indent <= 2:
            break
        scheme_match = re.fullmatch(r"    ([A-Za-z0-9_.-]+):\s*", line)
        if scheme_match:
            current_scheme = scheme_match.group(1)
            if current_scheme in scheme_blocks:
                raise ValueError(
                    f"duplicate OpenAPI security scheme: {current_scheme}"
                )
            scheme_blocks[current_scheme] = []
            continue
        if current_scheme is None:
            raise ValueError("OpenAPI security scheme content appeared before a scheme")
        if indent < 6:
            raise ValueError("invalid OpenAPI security scheme indentation")
        scheme_blocks[current_scheme].append(line)

    return default_security, {
        name: (
            canonical_yaml_block(values, child_indent(values))
            if values
            else ()
        )
        for name, values in scheme_blocks.items()
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
