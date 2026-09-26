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

    def finish_operation() -> None:
        nonlocal current_method, current_operation_id
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

    for line in lines[start:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        path_match = _PATH.fullmatch(line)
        if path_match:
            finish_operation()
            current_path = path_match.group(1)
            continue
        method_match = _METHOD.fullmatch(line)
        if method_match and method_match.group(1) in _HTTP_METHODS:
            finish_operation()
            if current_path is None:
                raise ValueError("OpenAPI method appeared before a path")
            current_method = method_match.group(1)
            current_operation_id = None
            continue
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
            lines.append(
                f"    public const string {name} = {_csharp_string(relative)};"
            )
            lines.append("")
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
