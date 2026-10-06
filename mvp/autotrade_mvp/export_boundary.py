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
_MAX_EXPORT_BYTES = 4 * 1024 * 1024
_MAX_EXPORT_DEPTH = 32
_MAX_EXPORT_ITEMS = 100_000
_MAX_INTEGER_BITS = 4_096
_MAX_INTEGER_DECIMAL_DIGITS = 1_234


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


def _utf8_text(value: object, *, name: str, allow_empty: bool = True) -> str:
    if type(value) is not str:
        raise ExportBoundaryError(f"{name} must be exact text")
    if len(value) > _MAX_EXPORT_BYTES:
        raise ExportBoundaryError(f"{name} exceeds export text hard limit")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ExportBoundaryError(f"{name} must contain valid UTF-8 text") from error
    if not allow_empty and not value:
        raise ExportBoundaryError(f"{name} is required")
    return value


def _required_text(value: object, *, name: str) -> str:
    value = _utf8_text(value, name=name)
    if not value.strip():
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
    if type(rights) is not dict:
        raise ExportBoundaryError("rights must be an exact object")
    if rights.get("export") is not True:
        raise PermissionError("rights do not permit export")
    return _required_text(rights.get("rights_id"), name="rights.rights_id")


class _Budget:
    def __init__(self, maximum_items: int):
        if type(maximum_items) is not int or maximum_items < 1:
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

    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if value.bit_length() > _MAX_INTEGER_BITS:
            raise ExportBoundaryError("integer exceeds export numeric hard limit")
        return value
    if type(value) is str:
        return _utf8_text(value, name="payload text")
    if type(value) is Decimal:
        if not value.is_finite():
            raise ExportBoundaryError("Decimal values must be finite")
        return format(value, "f")
    if type(value) is float:
        raise ExportBoundaryError("binary floating-point values are not export-safe")
    if type(value) is dict:
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str or not key:
                raise ExportBoundaryError("object keys must be non-empty exact strings")
            _utf8_text(key, name="object key", allow_empty=False)
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
    if type(value) in (list, tuple):
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
    max_bytes: int = _MAX_EXPORT_BYTES,
    max_depth: int = _MAX_EXPORT_DEPTH,
    maximum_items: int = _MAX_EXPORT_ITEMS,
) -> PreparedExport:
    """Prepare an inert, redacted, rights-aware JSON export.

    No arbitrary bytes, pickle/model objects, HTML or executable file formats are
    accepted. Strings are serialized verbatim as data; their wording cannot add
    tools, authority or credential access.
    """

    if type(max_bytes) is not int or max_bytes < 1:
        raise ExportBoundaryError("max_bytes must be a positive integer")
    if type(max_depth) is not int or max_depth < 1:
        raise ExportBoundaryError("max_depth must be a positive integer")
    if type(maximum_items) is not int or maximum_items < 1:
        raise ExportBoundaryError("maximum_items must be a positive integer")
    if max_bytes > _MAX_EXPORT_BYTES:
        raise ExportBoundaryError("max_bytes exceeds export boundary hard limit")
    if max_depth > _MAX_EXPORT_DEPTH:
        raise ExportBoundaryError("max_depth exceeds export boundary hard limit")
    if maximum_items > _MAX_EXPORT_ITEMS:
        raise ExportBoundaryError("maximum_items exceeds export boundary hard limit")

    if type(source_refs) not in (tuple, list):
        raise ExportBoundaryError("source_refs must be an exact collection")
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


def _parse_json_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > _MAX_INTEGER_DECIMAL_DIGITS:
        raise ExportBoundaryError("serialized integer exceeds numeric hard limit")
    parsed = int(value)
    if parsed.bit_length() > _MAX_INTEGER_BITS:
        raise ExportBoundaryError("serialized integer exceeds numeric hard limit")
    return parsed


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExportBoundaryError("serialized JSON contains duplicate object key")
        result[key] = value
    return result


def _serialized_payload_is_safe(
    value: object,
    *,
    depth: int,
    max_depth: int,
    budget: _Budget,
) -> bool:
    """Revalidate serialized JSON without trusting PreparedExport provenance."""

    if depth > max_depth:
        return False
    budget.consume()
    if value is None or type(value) is bool:
        return True
    if type(value) is int:
        return value.bit_length() <= _MAX_INTEGER_BITS
    if type(value) is str:
        try:
            _utf8_text(value, name="serialized text")
        except ExportBoundaryError:
            return False
        return True
    if type(value) is Decimal:
        # json.loads(parse_float=Decimal) exposes forbidden binary-style JSON
        # numeric fractions. Exact financial decimals must have been strings.
        return False
    if type(value) is list:
        return all(
            _serialized_payload_is_safe(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                budget=budget,
            )
            for item in value
        )
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key:
                return False
            try:
                _utf8_text(key, name="serialized object key", allow_empty=False)
            except ExportBoundaryError:
                return False
            if _SENSITIVE_KEY.search(key):
                if item != "[REDACTED]":
                    return False
                continue
            if not _serialized_payload_is_safe(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                budget=budget,
            ):
                return False
        return True
    return False


def verify_prepared_export(export: PreparedExport) -> bool:
    if type(export) is not PreparedExport:
        raise TypeError("export must be an exact PreparedExport")
    try:
        if type(export.media_type) is not str or export.media_type != "application/json":
            return False
        _export_id(export.export_id)
        _safe_filename(export.filename)
        _required_text(export.rights_id, name="rights_id")
        if type(export.source_refs) is not tuple:
            return False
        normalized_sources = tuple(
            _required_text(item, name="source_ref") for item in export.source_refs
        )
        if len(normalized_sources) != len(set(normalized_sources)):
            return False
        if type(export.data) is not bytes:
            return False
        if len(export.data) > _MAX_EXPORT_BYTES:
            return False
        if (
            type(export.sha256) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", export.sha256) is None
        ):
            return False
        decoded = export.data.decode("utf-8")
        parsed = json.loads(
            decoded,
            parse_float=Decimal,
            parse_int=_parse_json_int,
            object_pairs_hook=_unique_json_object,
        )
        if not _serialized_payload_is_safe(
            parsed,
            depth=0,
            max_depth=_MAX_EXPORT_DEPTH,
            budget=_Budget(_MAX_EXPORT_ITEMS),
        ):
            return False
    except (
        UnicodeError,
        json.JSONDecodeError,
        ExportBoundaryError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        return False
    return export.sha256 == f"sha256:{sha256(export.data).hexdigest()}"


def write_prepared_export(
    export: PreparedExport,
    directory: str | Path,
    *,
    rights: Mapping[str, object],
) -> Path:
    """Atomically publish a verified export beneath one caller-owned directory.

    Publication rechecks current export permission and binds it to the exact
    rights identity captured when the inert bytes were prepared.
    """

    if type(export) is not PreparedExport:
        raise TypeError("export must be an exact PreparedExport")
    current_rights_id = _rights(rights)
    if not verify_prepared_export(export):
        raise ExportBoundaryError("prepared export failed integrity verification")
    if current_rights_id != export.rights_id:
        raise PermissionError("publication rights identity does not match prepared export")
    if type(directory) is str:
        root = Path(directory)
    elif type(directory) is type(Path()):
        root = directory
    else:
        raise TypeError("directory must be an exact str or platform Path")
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
