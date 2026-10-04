import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from autotrade_research.artifacts.store import ArtifactStore
from autotrade_research.artifacts import _retained_namespace_hardening as retained


@unittest.skipIf(
    sys.platform == "win32",
    "non-Windows portability bridge characterization",
)
class ArtifactPortabilityBridgeTests(unittest.TestCase):
    def test_path_level_windows_descriptor_characterization_uses_local_predicate(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "store")
            artifact_id = str(uuid4())
            payload = b"targeted-portability-bridge"
            manifest = store.publish_bytes(
                artifact_id=artifact_id,
                data=payload,
                media_type="application/octet-stream",
                rights={"storage": True, "export": False},
            )
            canonical = store._object_path(
                manifest["sha256"].removeprefix("sha256:")
            )
            real_descriptor = os.open(
                canonical,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
            descriptor = None
            try:
                with patch.object(
                    retained,
                    "_use_path_level_windows_descriptor_bridge",
                    return_value=True,
                ), patch.object(
                    retained._store,
                    "_open_read_only_descriptor",
                    return_value=real_descriptor,
                ) as safe_open:
                    descriptor, opened = store._open_object_descriptor(
                        canonical,
                        expected_bytes=len(payload),
                    )
                self.assertEqual(descriptor, real_descriptor)
                self.assertEqual(opened.st_size, len(payload))
                safe_open.assert_called_once_with(canonical)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                else:
                    os.close(real_descriptor)


if __name__ == "__main__":
    unittest.main()
