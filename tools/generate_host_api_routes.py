"""Generate host API route bindings from the canonical OpenAPI document.

The parser intentionally supports only the conservative route subset used by
AutoTrade's reviewed host boundary. Unsupported or ambiguous path syntax fails
closed instead of being approximated. CI runs --check so desktop and web
clients cannot silently drift from contracts/openapi/host-api.yaml.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "contracts" / "openapi" / "host-api.yaml"
OUTPUTS = {
    "csharp": ROOT / "src" / "AutoTrade.Contracts" / "HostApiRoutes.cs",
    "web": ROOT / "web" / "src" / "host-api-routes.js",
}
_HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
)
_OPERATION_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_PATH = re.compile(r"^  (/[^\s:]*):\s*$")
_METHOD = re.compile(r"^    ([a-z]+):\s*$")
_OPERATION = re.compile(r"^      operationId:\s*([^\s#]+)\s*$")
_PARAMETER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    operation_id: str
    parameters: tuple[str, ...]


def _declared_path_parameters(lines: list[str]) -> tuple[str, ...]:
    """Parse the conservative direct operation-level parameter subset.

    Route generation must not infer authority from a path template alone. Every
    placeholder must be backed by one direct OpenAPI in:path declaration with
    the same name and required:true. References and path-level parameters are
    deliberately unsupported here instead of being approximated.
    """

    markers = [
        index for index, line in enumerate(lines)
        if line == "      parameters:"
    ]
    if len(markers) > 1:
        raise ValueError("OpenAPI operation contains duplicate parameters blocks")
    if not markers:
        return ()

    body: list[str] = []
    for line in lines[markers[0] + 1:]:
        if line and not line.startswith("        "):
            break
        body.append(line)

    entries: list[list[str]] = []
    current: list[str] = []
    for line in body:
        if not line.strip():
            continue
        if line.startswith("        - "):
            if current:
                entries.append(current)
            current = [line]
            continue
        if not current:
            raise ValueError(
                "unsupported OpenAPI operation parameter syntax"
            )
        if not line.startswith("          "):
            raise ValueError(
                "unsupported OpenAPI operation parameter indentation"
            )
        current.append(line)
    if current:
        entries.append(current)

    path_parameters: list[str] = []
    for entry in entries:
        kind_match = re.fullmatch(r"        - in: ([a-z]+)", entry[0])
        if kind_match is None:
            raise ValueError(
                "unsupported OpenAPI operation parameter syntax; "
                "parameter entries must begin with canonical '- in:'"
            )
        direct: dict[str, str] = {}
        schema: dict[str, str] = {}
        in_schema = False
        for line in entry[1:]:
            if line == "          schema:":
                if in_schema or "schema" in direct:
                    raise ValueError(
                        "duplicate OpenAPI operation parameter schema"
                    )
                direct["schema"] = "mapping"
                in_schema = True
                continue
            if line.startswith("            "):
                if not in_schema:
                    raise ValueError(
                        "nested OpenAPI parameter content appeared outside schema"
                    )
                schema_match = re.fullmatch(
                    r"            ([A-Za-z_][A-Za-z0-9_]*):\s*([^\s#]+)\s*",
                    line,
                )
                if schema_match is None:
                    raise ValueError(
                        "unsupported OpenAPI parameter schema field syntax"
                    )
                key, value = schema_match.groups()
                if key in schema:
                    raise ValueError(
                        f"duplicate OpenAPI parameter schema field: {key}"
                    )
                schema[key] = value
                continue
            in_schema = False
            field_match = re.fullmatch(
                r"          ([A-Za-z_][A-Za-z0-9_]*):\s*([^\s#]+)\s*",
                line,
            )
            if field_match is None:
                raise ValueError(
                    "unsupported OpenAPI operation parameter field syntax"
                )
            key, value = field_match.groups()
            if key in direct:
                raise ValueError(
                    f"duplicate OpenAPI operation parameter field: {key}"
                )
            direct[key] = value

        if kind_match.group(1) != "path":
            continue
        name = direct.get("name")
        if (
            name is None
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
        ):
            raise ValueError("OpenAPI path parameter has no canonical name")
        if direct.get("required") != "true":
            raise ValueError(
                f"OpenAPI path parameter {name} must be required: true"
            )
        if schema.get("type") != "string":
            raise ValueError(
                f"OpenAPI path parameter {name} must use schema type: string"
            )
        if name in path_parameters:
            raise ValueError(f"duplicate OpenAPI path parameter declaration: {name}")
        path_parameters.append(name)
    return tuple(path_parameters)


def parse_operations(text: str) -> tuple[Operation, ...]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("OpenAPI document must be non-empty text")
    lines = text.splitlines()
    path_markers = [
        index for index, line in enumerate(lines) if line == "paths:"
    ]
    if len(path_markers) != 1:
        raise ValueError(
            "OpenAPI document must contain exactly one top-level paths block"
        )

    start = path_markers[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        if line and not line.startswith(" ") and not line.lstrip().startswith("#"):
            end = index
            break

    operations: list[Operation] = []
    seen_pairs: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    current_path: str | None = None
    current_method: str | None = None
    current_operation_id: str | None = None
    current_operation_lines: list[str] = []

    def finish_operation() -> None:
        nonlocal current_method, current_operation_id, current_operation_lines
        if current_method is None:
            return
        if current_path is None:
            raise ValueError("OpenAPI operation appeared without a path")
        if current_operation_id is None:
            raise ValueError(
                f"OpenAPI operation {current_method.upper()} "
                f"{current_path} is missing operationId"
            )
        if _OPERATION_ID.fullmatch(current_operation_id) is None:
            raise ValueError(f"unsupported operationId: {current_operation_id}")
        pair = (current_method, current_path)
        if pair in seen_pairs:
            raise ValueError(
                "duplicate OpenAPI operation: "
                f"{current_method.upper()} {current_path}"
            )
        if current_operation_id in seen_ids:
            raise ValueError(
                f"duplicate OpenAPI operationId: {current_operation_id}"
            )
        parameters = tuple(_PARAMETER.findall(current_path))
        if len(parameters) != len(set(parameters)):
            raise ValueError(f"duplicate path parameter in {current_path}")
        residue = _PARAMETER.sub("", current_path)
        if "{" in residue or "}" in residue:
            raise ValueError(f"unsupported path-template syntax: {current_path}")
        declared_parameters = _declared_path_parameters(current_operation_lines)
        if set(parameters) != set(declared_parameters):
            raise ValueError(
                "OpenAPI path template parameters do not match direct required "
                f"path parameter declarations for {current_method.upper()} "
                f"{current_path}: template={sorted(parameters)} "
                f"declared={sorted(declared_parameters)}"
            )
        if not current_path.startswith("/api/v1/"):
            raise ValueError(
                "host route must use canonical /api/v1/ prefix: "
                f"{current_path}"
            )
        seen_pairs.add(pair)
        seen_ids.add(current_operation_id)
        operations.append(
            Operation(
                method=current_method,
                path=current_path,
                operation_id=current_operation_id,
                parameters=parameters,
            )
        )
        current_method = None
        current_operation_id = None
        current_operation_lines = []

    for line in lines[start:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        path_match = _PATH.fullmatch(line)
        if path_match:
            finish_operation()
            current_path = path_match.group(1)
            continue
        if line.startswith("  ") and not line.startswith("    "):
            candidate = line.strip()
            if (
                candidate.startswith("/")
                or candidate.startswith('"/')
                or candidate.startswith("'/")
            ):
                raise ValueError(
                    "unsupported OpenAPI path entry syntax; "
                    "path keys must use canonical unquoted form"
                )
        method_match = _METHOD.fullmatch(line)
        if method_match and method_match.group(1) in _HTTP_METHODS:
            finish_operation()
            if current_path is None:
                raise ValueError("OpenAPI method appeared before a path")
            current_method = method_match.group(1)
            current_operation_id = None
            current_operation_lines = []
            continue
        if (
            current_path is not None
            and current_method is None
            and line == "    parameters:"
        ):
            raise ValueError(
                "path-level OpenAPI parameters are unsupported by route generation"
            )
        if line.startswith("    ") and not line.startswith("      "):
            candidate = line.strip()
            raw_key = candidate.split(":", 1)[0].strip()
            dequoted_key = raw_key
            if (
                len(raw_key) >= 2
                and raw_key[0] == raw_key[-1]
                and raw_key[0] in {'"', "'"}
            ):
                dequoted_key = raw_key[1:-1]
            if dequoted_key.lower() in _HTTP_METHODS:
                raise ValueError(
                    "unsupported OpenAPI HTTP operation syntax; "
                    "method keys must be canonical lowercase unquoted entries "
                    "without inline content"
                )
        if current_method is not None:
            current_operation_lines.append(line)

        operation_match = _OPERATION.fullmatch(line)
        if operation_match:
            if current_method is None:
                raise ValueError("operationId appeared outside an HTTP operation")
            if current_operation_id is not None:
                raise ValueError(
                    "duplicate operationId field for "
                    f"{current_method.upper()} {current_path}"
                )
            current_operation_id = operation_match.group(1)

    finish_operation()
    if not operations:
        raise ValueError("OpenAPI paths block contains no supported operations")
    return tuple(operations)


def load_operations() -> tuple[Operation, ...]:
    return parse_operations(OPENAPI.read_text(encoding="utf-8"))


def _pascal_case(value: str) -> str:
    return value[0].upper() + value[1:]


def _camel_parameter(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(
        part[:1].upper() + part[1:] for part in parts[1:]
    )


def _csharp_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_csharp(operations: tuple[Operation, ...]) -> str:
    lines = [
        "namespace AutoTrade.Contracts;",
        "",
        "/// <summary>",
        "/// AUTO-GENERATED from contracts/openapi/host-api.yaml. DO NOT EDIT.",
        "/// Run python tools/generate_host_api_routes.py to regenerate.",
        "/// Route values are relative to the validated host origin.",
        "/// </summary>",
        "public static class HostApiRoutes",
        "{",
    ]
    for operation in operations:
        name = _pascal_case(operation.operation_id)
        relative = operation.path.removeprefix("/")
        if not operation.parameters:
            lines.extend(
                [
                    "    /// <summary>",
                    (
                        "    /// Relative route for OpenAPI operation "
                        f"{operation.operation_id}."
                    ),
                    "    /// </summary>",
                    f"    public const string {name} = {_csharp_string(relative)};",
                    "",
                ]
            )
            continue

        parameter_names = {
            parameter: _camel_parameter(parameter)
            for parameter in operation.parameters
        }
        if len(set(parameter_names.values())) != len(parameter_names):
            raise ValueError(
                f"path parameter names collide in C#: {operation.path}"
            )
        signature = ", ".join(
            f"string {parameter_names[item]}"
            for item in operation.parameters
        )
        lines.extend(
            [
                "    /// <summary>",
                (
                    "    /// Resolve the relative route for OpenAPI operation "
                    f"{operation.operation_id}."
                ),
                "    /// </summary>",
            ]
        )
        for parameter in operation.parameters:
            local = parameter_names[parameter]
            lines.append(
                f'    /// <param name="{local}">Canonical value for '
                f"{parameter}.</param>"
            )
        lines.append(
            "    /// <returns>The relative route with encoded path parameters.</returns>"
        )
        lines.append(f"    public static string {name}({signature})")
        lines.append("    {")
        for parameter in operation.parameters:
            local = parameter_names[parameter]
            lines.extend(
                [
                    (
                        f"        if (string.IsNullOrWhiteSpace({local}) "
                        f"|| !string.Equals({local}, {local}.Trim(), "
                        "StringComparison.Ordinal))"
                    ),
                    "        {",
                    "            throw new ArgumentException(",
                    (
                        f'                "Host API route parameter {parameter} '
                        'is required and must be canonical.",'
                    ),
                    f"                nameof({local}));",
                    "        }",
                    "",
                ]
            )

        pieces: list[str] = []
        cursor = 0
        for match in _PARAMETER.finditer(relative):
            literal = relative[cursor : match.start()]
            if literal:
                pieces.append(_csharp_string(literal))
            local = parameter_names[match.group(1)]
            pieces.append(f"Uri.EscapeDataString({local})")
            cursor = match.end()
        trailing = relative[cursor:]
        if trailing:
            pieces.append(_csharp_string(trailing))
        expression = " + ".join(pieces) if pieces else '""'
        lines.append(f"        return {expression};")
        lines.append("    }")
        lines.append("")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def render_web(operations: tuple[Operation, ...]) -> str:
    entries = ",\n".join(
        f"    {operation.operation_id}: {operation.path!r}"
        for operation in operations
    )
    return f"""(() => {{
  "use strict";

  // AUTO-GENERATED from contracts/openapi/host-api.yaml. DO NOT EDIT.
  // Run python tools/generate_host_api_routes.py to regenerate.
  const ROUTES = Object.freeze({{
{entries}
  }});

  function encodePathSegment(raw) {{
    return encodeURIComponent(raw).replace(
      /[!'()*]/g,
      (character) =>
        "%" + character.charCodeAt(0).toString(16).toUpperCase()
    );
  }}

  function route(operationId, parameters = {{}}) {{
    const template = ROUTES[operationId];
    if (typeof template !== "string") {{
      throw new RangeError(
        "Unknown host API operation: " + String(operationId));
    }}
    let value = template;
    const required = [
      ...template.matchAll(/\\{{([A-Za-z_][A-Za-z0-9_]*)\\}}/g)
    ].map((match) => match[1]);
    for (const name of required) {{
      const raw = parameters[name];
      if (
        typeof raw !== "string" ||
        raw.trim() === "" ||
        raw !== raw.trim()
      ) {{
        throw new TypeError(
          "Host API route parameter " + name +
          " is required and must be canonical");
      }}
      value = value.replace("{{" + name + "}}", encodePathSegment(raw));
    }}
    if (/\\{{|\\}}/.test(value)) {{
      throw new Error("Host API route template was not fully resolved");
    }}
    return value;
  }}

  Object.defineProperty(window, "AutoTradeHostApi", {{
    value: Object.freeze({{routes: ROUTES, route}}),
    writable: false,
    configurable: false,
    enumerable: false
  }});
}})();
"""


def rendered_outputs() -> dict[str, str]:
    operations = load_operations()
    return {
        "csharp": render_csharp(operations),
        "web": render_web(operations),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = rendered_outputs()
    stale: list[Path] = []
    for key, path in OUTPUTS.items():
        expected = rendered[key]
        if args.check:
            try:
                current = path.read_text(encoding="utf-8")
            except OSError:
                stale.append(path)
                continue
            if current != expected:
                stale.append(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected, encoding="utf-8")

    if args.check:
        if stale:
            for path in stale:
                print(
                    "generated host API route binding is stale: "
                    f"{path.relative_to(ROOT)}",
                    file=sys.stderr,
                )
            print(
                "run python tools/generate_host_api_routes.py",
                file=sys.stderr,
            )
            return 1
        print("Host API route bindings are OpenAPI-derived and current.")
        return 0

    for path in OUTPUTS.values():
        print(f"Wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
