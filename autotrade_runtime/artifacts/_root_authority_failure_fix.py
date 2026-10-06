from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import sys
import tempfile
import weakref

from . import _generation_bound_read as _generation
from . import _root_authority as _root

_store = _root._store
_ORIGINAL_TRUSTED_AUTHENTICATED_READER = _root.trusted_authenticated_reader


def _duplicate_exact_store_generation_pins(store: object) -> tuple[int, ...]:
    """Duplicate retained namespace capabilities without caller coercion."""

    if type(store) is not _store.ArtifactStore:
        raise TypeError("trusted generation requires canonical ArtifactStore")
    attributes = (
        ("root", "_namespace_root_handle" if sys.platform == "win32" else "_namespace_root_fd"),
        (
            "manifests",
            "_retained_manifests_handle"
            if sys.platform == "win32"
            else "_retained_manifests_fd",
        ),
        (
            "objects",
            "_retained_objects_handle"
            if sys.platform == "win32"
            else "_retained_objects_fd",
        ),
        (
            "staging",
            "_retained_staging_handle"
            if sys.platform == "win32"
            else "_retained_staging_fd",
        ),
    )
    source: list[int] = []
    for name, attribute in attributes:
        try:
            capability = object.__getattribute__(store, attribute)
        except AttributeError:
            capability = None
        if (
            type(capability) is not int
            or capability < 0
            or (sys.platform == "win32" and capability == 0)
        ):
            raise _store.ArtifactIntegrityError(
                f"retained artifact {name} capability is unavailable"
            )
        source.append(capability)
    return _root._duplicate_generation_pins(tuple(source))


def _assert_same_namespace_generation(
    publication_store: object,
    private_store: object,
) -> None:
    """Require root and retained child capabilities to name one generation."""

    if type(publication_store) is not _store.ArtifactStore:
        raise TypeError("publication_store must be the canonical ArtifactStore")
    if type(private_store) is not _store.ArtifactStore:
        raise TypeError("private trusted reader must use canonical ArtifactStore")

    publication_pins: tuple[int, ...] = ()
    private_pins: tuple[int, ...] = ()
    try:
        publication_pins = _duplicate_exact_store_generation_pins(publication_store)
        private_pins = _duplicate_exact_store_generation_pins(private_store)
        publication_generation = _root._pinned_generation(publication_pins)
        private_generation = _root._pinned_generation(private_pins)
        if publication_generation != private_generation:
            raise _store.ArtifactIntegrityError(
                "publication store does not match trusted artifact namespace generation"
            )
    finally:
        _root._close_generation_pins(private_pins)
        _root._close_generation_pins(publication_pins)


def _publication_bound_trusted_authenticated_reader(
    authoritative_root: str | Path,
    *,
    publication_store: object | None = None,
):
    """Pin an existing publication generation without reconstructing its store."""

    if publication_store is None:
        return _ORIGINAL_TRUSTED_AUTHENTICATED_READER(authoritative_root)

    root = _root._canonical_authoritative_root(authoritative_root)
    root_key = _root._configured_root_key(root)
    pins: tuple[int, ...] = ()
    registered_reader_id: int | None = None
    try:
        pins = _duplicate_exact_store_generation_pins(publication_store)
        expected_generation = _root._pinned_generation(pins)
        configured_generation = _root._immutable_configured_generation(root_key)
        if configured_generation != expected_generation:
            raise _store.ArtifactIntegrityError(
                "publication store does not match trusted artifact root"
            )

        reader = _root._TrustedAuthenticatedReader()
        reader_id = id(reader)
        with _root._READER_CAPABILITY_LOCK:
            if reader_id in _root._READER_CAPABILITIES:
                raise _store.ArtifactIntegrityError(
                    "trusted reader capability identity collision"
                )
            _root._READER_CAPABILITIES[reader_id] = (root_key, pins)
        registered_reader_id = reader_id
        weakref.finalize(
            reader,
            _root._release_reader_capability,
            reader_id,
        )
        return reader
    except BaseException:
        if registered_reader_id is None:
            _root._close_generation_pins(pins)
        else:
            with _root._READER_CAPABILITY_LOCK:
                state = _root._READER_CAPABILITIES.pop(
                    registered_reader_id,
                    None,
                )
            if state is not None:
                _root_key, registered_pins = state
                _root._close_generation_pins(registered_pins)
        raise


def _install_publication_bound_trusted_reader() -> None:
    _root._assert_same_root_generation = _assert_same_namespace_generation
    _root.trusted_authenticated_reader = (
        _publication_bound_trusted_authenticated_reader
    )
    package = sys.modules.get(__package__)
    if package is not None:
        package.trusted_authenticated_reader = (
            _publication_bound_trusted_authenticated_reader
        )


def _raise_root_loss_if_any(self, primary: BaseException) -> None:
    try:
        _root._assert_root_continuity(self)
    except BaseException as authority_error:
        try:
            authority_error.add_note(
                f"inner artifact operation also failed: {type(primary).__name__}: {primary}"
            )
        except BaseException:
            pass
        raise authority_error from primary


def _exceptional_root_fence(method):
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except BaseException as primary:
            _raise_root_loss_if_any(self, primary)
            raise

    wrapped.__name__ = getattr(method, "__name__", "exceptional_root_fence")
    wrapped.__doc__ = getattr(method, "__doc__", None)
    return wrapped


def _export_linearized(self, artifact_id: str, destination: str | Path) -> Path:
    """Export one authenticated object with a single external commit point.

    Root continuity is authoritative up to the atomic destination replacement.
    Once that replacement succeeds, the export is committed and later lexical
    root replacement cannot retroactively turn the completed external side
    effect into a reported failure. Schema-v2 source reads additionally retain
    the manifest-bound object-prefix generation through the complete copy.
    """

    with _root._configured_path_coordination(self):
        _root._assert_root_continuity(self)
        prefix_authority = None
        try:
            manifest = self.load_manifest(artifact_id)
            _store._verify_manifest_integrity(manifest, required=True)
            if manifest.get("rights", {}).get("export") is not True:
                raise PermissionError("artifact rights do not permit export")
            if self._export_authorizer is None:
                raise PermissionError("independent export authorization is required")

            source, expected_digest, expected_bytes = self._manifest_object_contract(
                manifest
            )
            if manifest.get("schema_version") == 2:
                descriptor, opened, prefix_authority, prefix_opened = (
                    _generation._open_generation_bound_descriptor(
                        self,
                        manifest,
                        source,
                        expected_bytes=expected_bytes,
                    )
                )
            else:
                descriptor, opened = self._open_object_descriptor(
                    source,
                    expected_bytes=expected_bytes,
                )
                prefix_opened = None
            try:
                try:
                    authorized = self._export_authorizer(
                        manifest["artifact_id"],
                        manifest["sha256"],
                    )
                except Exception as error:
                    raise PermissionError(
                        "independent export authorization failed closed"
                    ) from error
                if authorized is not True:
                    raise PermissionError(
                        "independent export authority does not permit export"
                    )

                target = Path(destination)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary: Path | None = None
                committed = False
                try:
                    with tempfile.NamedTemporaryFile(
                        "wb",
                        dir=target.parent,
                        prefix=f".{target.name}.",
                        suffix=".tmp",
                        delete=False,
                    ) as handle:
                        temporary = Path(handle.name)
                        copied = 0
                        copied_hash = sha256()
                        for chunk in self._bounded_descriptor_chunks(
                            descriptor,
                            expected_bytes,
                        ):
                            handle.write(chunk)
                            copied += len(chunk)
                            copied_hash.update(chunk)
                        if manifest.get("schema_version") == 2:
                            _generation._revalidate_generation_bound_descriptor(
                                self,
                                manifest,
                                source,
                                descriptor,
                                opened,
                                prefix_authority,
                                prefix_opened,
                                expected_bytes=expected_bytes,
                            )
                        else:
                            self._revalidate_object_descriptor(
                                source,
                                descriptor,
                                opened,
                                expected_bytes=expected_bytes,
                            )
                        handle.flush()
                        os.fsync(handle.fileno())
                    if (
                        copied != expected_bytes
                        or copied_hash.hexdigest() != expected_digest
                    ):
                        raise _store.ArtifactIntegrityError(
                            "artifact object changed during export copy"
                        )

                    # Last source/root authority fence. The following replace is
                    # the external commit point; no post-commit root check may
                    # report failure after the durable side effect exists.
                    _root._assert_root_continuity(self)
                    os.replace(temporary, target)
                    temporary = None
                    committed = True
                    _store.sync_parent_directory(target)
                finally:
                    if temporary is not None:
                        try:
                            temporary.unlink()
                        except FileNotFoundError:
                            pass
                if not committed:
                    raise _store.ArtifactIntegrityError(
                        "artifact export did not reach its commit point"
                    )
                return target
            finally:
                os.close(descriptor)
                if prefix_authority is not None:
                    _generation._close_prefix_authority(prefix_authority)
                    prefix_authority = None
        except BaseException as primary:
            # Before destination commit, root loss outranks a lower-level error.
            # After commit the function has already returned through the success
            # path and deliberately performs no retroactive root fence.
            _raise_root_loss_if_any(self, primary)
            raise


def install_root_authority_failure_fix() -> None:
    artifact_store = _store.ArtifactStore
    _install_publication_bound_trusted_reader()
    if getattr(artifact_store, "_root_authority_failure_fix", False):
        return

    artifact_store.load_manifest = _exceptional_root_fence(
        artifact_store.load_manifest
    )
    artifact_store.read_authenticated_snapshot = _exceptional_root_fence(
        artifact_store.read_authenticated_snapshot
    )
    # Root authority pins the terminal authenticated-read dispatch so later
    # public-class rebinding cannot redirect issued readers. Refresh that pin
    # only after this final exceptional-root wrapper is installed; otherwise
    # trusted readers silently bypass the canonical failure-path root fence.
    _root._CANONICAL_AUTHENTICATED_READ = (
        artifact_store.read_authenticated_snapshot
    )
    artifact_store.read_bytes = _exceptional_root_fence(artifact_store.read_bytes)
    artifact_store.audit = _exceptional_root_fence(artifact_store.audit)
    artifact_store.publish_bytes = _exceptional_root_fence(
        artifact_store.publish_bytes
    )
    artifact_store.recover_orphans = _exceptional_root_fence(
        artifact_store.recover_orphans
    )
    artifact_store.export = _export_linearized
    artifact_store._root_authority_failure_fix = True
