from __future__ import annotations

from pathlib import Path

from . import _namespace_guard as _guard
from . import _retained_namespace as _retained

_store = _retained._store


def _concrete_path(path: Path) -> Path:
    return path if isinstance(path, Path) else Path(path)


def _validate_manifest_name(store, manifest_path: Path) -> str:
    path = _concrete_path(manifest_path)
    if path.parent != store.manifests:
        raise _store.ArtifactIntegrityError(
            "artifact manifest path escapes store namespace"
        )
    return _guard._validate_component(
        path.name,
        subject="artifact manifest",
    )


def _validate_object_name(store, object_path: Path) -> tuple[str, str]:
    path = _concrete_path(object_path)
    digest = path.name
    prefix = path.parent.name
    if path.parent.parent != store.objects:
        raise _store.ArtifactIntegrityError(
            "content-addressed object path escapes store namespace"
        )
    try:
        canonical = store._object_path(digest)
    except ValueError as error:
        raise _store.ArtifactIntegrityError(
            "content-addressed object digest is invalid"
        ) from error
    if canonical != path or prefix != digest[:2]:
        raise _store.ArtifactIntegrityError(
            "content-addressed object path is not canonical"
        )
    return (
        _guard._validate_component(prefix, subject="artifact object"),
        _guard._validate_component(digest, subject="artifact object"),
    )


def install_retained_path_compatibility() -> None:
    _retained._validate_manifest_name = _validate_manifest_name
    _retained._validate_object_name = _validate_object_name
