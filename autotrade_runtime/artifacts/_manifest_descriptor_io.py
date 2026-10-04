from __future__ import annotations

from io import FileIO
import importlib
import os
from pathlib import Path

_store = importlib.import_module(f"{__package__}.store")


def _isolated_read_manifest_descriptor(
    self,
    manifest_path: Path,
    descriptor: int,
    opened: os.stat_result,
) -> bytes:
    """Read held manifest bytes without sharing the object-byte read primitive.

    ArtifactStore tests and consumers need to distinguish "the manifest was read"
    from "artifact object bytes were consumed".  Object payload reads intentionally
    remain on ``os.read`` through ``_bounded_descriptor_chunks``; manifest reads use
    a raw ``FileIO`` wrapper over the already-authenticated held descriptor.  This
    keeps both reads descriptor-bound while making object-read instrumentation
    unable to perturb the manifest authority itself.
    """

    expected_bytes = opened.st_size
    remaining = expected_bytes + 1
    chunks: list[bytes] = []
    copied = 0
    try:
        with FileIO(descriptor, mode="rb", closefd=False) as reader:
            while remaining > 0:
                chunk = reader.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise _store.ArtifactIntegrityError(
                        "artifact manifest reader returned non-bytes data"
                    )
                chunks.append(chunk)
                copied += len(chunk)
                remaining -= len(chunk)
    except OSError as error:
        raise _store.ArtifactIntegrityError(
            "artifact manifest could not be read safely"
        ) from error

    self._revalidate_manifest_descriptor(
        manifest_path,
        descriptor,
        opened,
    )
    if copied != expected_bytes:
        raise _store.ArtifactIntegrityError(
            "artifact manifest changed during read"
        )
    return b"".join(chunks)


def install_manifest_descriptor_reader() -> None:
    _store.ArtifactStore._read_manifest_descriptor = (
        _isolated_read_manifest_descriptor
    )
