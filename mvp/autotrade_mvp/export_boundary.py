"""Fail-closed host export boundary for untrusted structured data.

This module creates only inert JSON exports. It does not render HTML, execute
plugins/models, resolve credentials, or grant trading authority. Payload strings
remain data even when they contain instructions aimed at a model or operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping
from uuid import UUID


_SENSITIVE_KEY = re.compile(
    r"(authorization|cookie|password|passphrase|secret|session|token|"
    r"api[_-]?key|private[_-]?key|credential|signature|access[_-]?token|"
    r"refresh[_-]?token)",
    re.IGNORECASE,
)
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ExportBoundaryError(ValueError):
    """Raised when an export cannot be proven safe for this boundary."""


@dataclass(frozen=True)
class PreparedExport:
    export_id: str
    filename: str
    media_type: str
    data: bytes
    sha256: str
    rights_id: str
    source_refs: tuple[str, ...]

    def manifest(self) -> dict[str, object]:
        return {
            "export_id": self.export_id,
            "filename": self.filename,
            "media_type": self.media_type,
            "bytes": len(self.data),
            "sha256": self.sha256,
            "rights_id": self.rights_id,
            "source_refs": list(self.source_refs),
            "credentials_included": False,
            "active_content": False,
        }


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExportBoundaryError(f"{name} is required")
    return value.strip()


def _export_id(value: object) -> str:
    text = _required_text(value, name="export_id")
    try:
        return str(UUID(text))
    except ValueError as error:
        raise ExportBoundaryError("export_id must be a UUID") from error


def _safe_filename(value: object) -> str:
    name = _required_text(value, name="filename")
    if len(name) > 128:
        raise ExportBoundaryError("filename is too long")
    if name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise ExportBoundaryError("filename must be a single safe path component")
    if any(ord(ch) < 32 for ch in name) or any(ch in '<>:"|?*' for ch in name):
        raise ExportBoundaryError("filename contains a Windows-invalid character")
    if name != name.strip(" ."):
        raise ExportBoundaryError("filename cannot start or end with a space or dot")
    if not name.lower().endswith(".json"):
        raise ExportBoundaryError("this boundary exports JSON files only")
    device_stem = name.split(".", 1)[0].upper()
    if device_stem in _WINDOWS_RESERVED:
        raise ExportBoundaryError("filename is reserved on Windows")
    return name


def _rights(rights: Mapping[str, object]) -> str:
    if not isinstance(rights, Mapping):
        raise ExportBoundaryError("rights must be an object")
    if rights.get("export") is not True:
        raise PermissionError("rights do not permit export")
    return _required_text(rights.get("rights_id"), name="rights.rights_id")


class _Budget:
    def __init__(self, maximum_items: int):
        if not isinstance(maximum_items, int) or isinstance(maximum_items, bool) or maximum_items < 1:
            raise ExportBoundaryError("maximum_items must be a positive integer")
        self.remaining = maximum_items

    def consume(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise ExportBoundaryError("payload exceeds item budget")


def _normalize(value: object, *, depth: int, max_depth: int, budget: _Budget) -> Any:
    if depth > max_depth:
        raise ExportBoundaryError("payload exceeds nesting limit")
    budget.consume()

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ExportBoundaryError("Decimal values must be finite")
        return format(value, "f")
    if isinstance(value, float):
        raise ExportBoundaryError("binary floating-point values are not export-safe")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ExportBoundaryError("object keys must be non-empty strings")
            if key in result:
                raise ExportBoundaryError("duplicate object key")
            if _SENSITIVE_KEY.search(key):
                result[key] = "[REDACTED]"
            else:
                result[key] = _normalize(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    budget=budget,
                )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _normalize(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                budget=budget,
            )
            for item in value
        ]
    raise ExportBoundaryError(
        f"unsupported export value type: {type(value).__name__}"
    )


def prepare_json_export(
    *,
    export_id: object,
    filename: object,
    payload: object,
    rights: Mapping[str, object],
    source_refs: tuple[str, ...] | list[str] = (),
    max_bytes: int = 4 * 1024 * 1024,
    max_depth: int = 32,
    maximum_items: int = 100_000,
) -> PreparedExport:
    """Prepare an inert, redacted, rights-aware JSON export.

    No arbitrary bytes, pickle/model objects, HTML or executable file formats are
    accepted. Strings are serialized verbatim as data; their wording cannot add
    tools, authority or credential access.
    """

    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ExportBoundaryError("max_bytes must be a positive integer")
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 1:
        raise ExportBoundaryError("max_depth must be a positive integer")

    normalized_sources = tuple(
        _required_text(item, name="source_ref") for item in source_refs
    )
    if len(normalized_sources) != len(set(normalized_sources)):
        raise ExportBoundaryError("source_refs must be unique")

    normalized = _normalize(
        payload,
        depth=0,
        max_depth=max_depth,
        budget=_Budget(maximum_items),
    )
    data = (
        json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(data) > max_bytes:
        raise ExportBoundaryError("prepared export exceeds byte limit")

    digest = sha256(data).hexdigest()
    return PreparedExport(
        export_id=_export_id(export_id),
        filename=_safe_filename(filename),
        media_type="application/json",
        data=data,
        sha256=f"sha256:{digest}",
        rights_id=_rights(rights),
        source_refs=normalized_sources,
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExportBoundaryError("serialized JSON contains duplicate object key")
        result[key] = value
    return result


def _serialized_payload_is_safe(value: object) -> bool:
    """Revalidate serialized JSON without trusting PreparedExport provenance."""

    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, Decimal):
        # json.loads(parse_float=Decimal) exposes forbidden binary-style JSON
        # numeric fractions. Exact financial decimals must have been strings.
        return False
    if isinstance(value, list):
        return all(_serialized_payload_is_safe(item) for item in value)
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                return False
            if _SENSITIVE_KEY.search(key):
                if item != "[REDACTED]":
                    return False
                continue
            if not _serialized_payload_is_safe(item):
                return False
        return True
    return False


def verify_prepared_export(export: PreparedExport) -> bool:
    if not isinstance(export, PreparedExport):
        raise TypeError("export must be PreparedExport")
    if export.media_type != "application/json":
        return False
    try:
        _export_id(export.export_id)
        _safe_filename(export.filename)
        _required_text(export.rights_id, name="rights_id")
        normalized_sources = tuple(
            _required_text(item, name="source_ref") for item in export.source_refs
        )
        if len(normalized_sources) != len(set(normalized_sources)):
            return False
        if not isinstance(export.data, bytes):
            return False
        decoded = export.data.decode("utf-8")
        parsed = json.loads(
            decoded,
            parse_float=Decimal,
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeError, json.JSONDecodeError, ExportBoundaryError, TypeError):
        return False
    if not _serialized_payload_is_safe(parsed):
        return False
    return export.sha256 == f"sha256:{sha256(export.data).hexdigest()}"


def write_prepared_export(export: PreparedExport, directory: str | Path) -> Path:
    """Atomically publish a verified export beneath one caller-owned directory."""

    if not verify_prepared_export(export):
        raise ExportBoundaryError("prepared export failed integrity verification")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    target = root / export.filename

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=root,
            prefix=f".{export.filename}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(export.data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return target
