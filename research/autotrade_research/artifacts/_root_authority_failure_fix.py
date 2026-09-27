from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import tempfile

from . import _generation_bound_read as _generation
from . import _root_authority as _root

_store = _root._store


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
    if getattr(artifact_store, "_root_authority_failure_fix", False):
        return

    artifact_store.load_manifest = _exceptional_root_fence(
        artifact_store.load_manifest
    )
    artifact_store.read_authenticated_snapshot = _exceptional_root_fence(
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
