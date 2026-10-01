from __future__ import annotations

from hashlib import sha256
import unittest

from tools.build_windows_install_manifest import (
    InstallerManifestError,
    _verify_composition,
)
from tools.stage_windows_foundation import _REQUIRED as _FOUNDATION_REQUIRED
from tools.stage_windows_release_runtime import _RELEASE_RUNTIME_REQUIRED
from tools.stage_windows_runtime import _RUNTIME_REQUIRED


SOURCE_SHA = "a" * 40


def _digest(label: str) -> str:
    return "sha256:" + sha256(label.encode("utf-8")).hexdigest()


def _base_entries() -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    files: list[dict[str, object]] = []
    components: list[dict[str, str]] = []
    for path, kind in (
        ("AutoTrade.Desktop.exe", "runtime"),
        ("dependency-lock.json", "dependency-lock"),
        ("sbom.spdx.json", "sbom"),
    ):
        digest = _digest(path)
        files.append(
            {
                "source_path": path,
                "target_relative_path": path,
                "sha256": digest,
                "size": len(path),
            }
        )
        components.append(
            {
                "component_id": path.replace("/", "-"),
                "kind": kind,
                "path": path,
                "version": "1.0.0",
                "sha256": digest,
            }
        )
    return files, components


def _composition(components: list[dict[str, str]]) -> dict[str, object]:
    by_path = {item["path"]: item for item in components}
    return {
        "schema_version": "1.0.0",
        "product": "AutoTrade",
        "source_sha": SOURCE_SHA,
        "dependency_lock_sha256": by_path["dependency-lock.json"]["sha256"],
        "sbom_sha256": by_path["sbom.spdx.json"]["sha256"],
        "schema_compatibility": {"minimum": "1.0.0", "maximum": "1.0.x"},
        "runtime": {
            "architecture": "x64",
            "runtime_identifier": "win-x64",
            "minimum_windows_version": "10.0.22621",
        },
        "components": components,
    }


def _append_release_runtime_inventory(
    files: list[dict[str, object]],
    components: list[dict[str, str]],
) -> None:
    for descriptor in _RELEASE_RUNTIME_REQUIRED:
        digest = _digest(descriptor.path)
        files.append(
            {
                "source_path": descriptor.path,
                "target_relative_path": descriptor.path,
                "sha256": digest,
                "size": len(descriptor.path),
            }
        )
        components.append(
            {
                "component_id": descriptor.component_id,
                "kind": descriptor.kind,
                "path": descriptor.path,
                "version": "source-controlled",
                "sha256": digest,
            }
        )


def _append_product_entrypoints(
    files: list[dict[str, object]],
    components: list[dict[str, str]],
) -> None:
    for component_id, kind, path in (
        (
            "autotrade-desktop-entrypoint",
            "desktop-entrypoint",
            "product/AutoTrade.Desktop.exe",
        ),
        (
            "autotrade-host-entrypoint",
            "host-entrypoint",
            "product/AutoTrade.Host.exe",
        ),
    ):
        digest = _digest(component_id)
        files.append(
            {
                "source_path": path,
                "target_relative_path": path,
                "sha256": digest,
                "size": len(component_id),
            }
        )
        components.append(
            {
                "component_id": component_id,
                "kind": kind,
                "path": path,
                "version": "1.0.0",
                "sha256": digest,
            }
        )


def _verified(
    files: list[dict[str, object]],
    components: list[dict[str, str]],
) -> dict[str, object]:
    return _verify_composition(
        _composition(components),
        source_sha=SOURCE_SHA,
        verified_files=files,
    )


class NeutralRuntimeInstallerInventoryCandidateTests(unittest.TestCase):
    """Terminal installer requires exact release runtime and product roles."""

    def test_release_installer_rejects_composition_with_no_neutral_runtime(self):
        files, components = _base_entries()
        _append_product_entrypoints(files, components)
        with self.assertRaisesRegex(InstallerManifestError, "release runtime"):
            _verified(files, components)

    def test_release_installer_rejects_one_missing_runtime_leaf(self):
        files, components = _base_entries()
        _append_product_entrypoints(files, components)
        _append_release_runtime_inventory(files, components)
        missing = _RUNTIME_REQUIRED[-1].path
        files[:] = [item for item in files if item["target_relative_path"] != missing]
        components[:] = [item for item in components if item["path"] != missing]
        with self.assertRaisesRegex(InstallerManifestError, "release runtime"):
            _verified(files, components)

    def test_release_installer_rejects_one_missing_foundation_leaf(self):
        files, components = _base_entries()
        _append_product_entrypoints(files, components)
        _append_release_runtime_inventory(files, components)
        missing = _FOUNDATION_REQUIRED[-1].path
        files[:] = [item for item in files if item["target_relative_path"] != missing]
        components[:] = [item for item in components if item["path"] != missing]
        with self.assertRaisesRegex(InstallerManifestError, "release runtime"):
            _verified(files, components)

    def test_release_installer_rejects_research_alias_in_place_of_canonical_runtime_leaf(self):
        files, components = _base_entries()
        _append_product_entrypoints(files, components)
        _append_release_runtime_inventory(files, components)
        canonical = "autotrade_runtime/artifacts/store.py"
        alias = "research/autotrade_research/artifacts/content_store.py"
        for item in files:
            if item["target_relative_path"] == canonical:
                item["source_path"] = alias
                item["target_relative_path"] = alias
                item["sha256"] = _digest(alias)
                break
        for item in components:
            if item["path"] == canonical:
                item["component_id"] = "research-artifact-store-alias"
                item["kind"] = "runtime-artifacts"
                item["path"] = alias
                item["sha256"] = _digest(alias)
                break
        with self.assertRaisesRegex(InstallerManifestError, "release runtime"):
            _verified(files, components)

    def test_release_installer_rejects_noncanonical_runtime_component_identity(self):
        cases = (
            ("component_id", "forged-runtime-component"),
            ("kind", "asset"),
            ("version", "1.0.0"),
        )
        for field, replacement in cases:
            with self.subTest(field=field):
                files, components = _base_entries()
        _append_product_entrypoints(files, components)
                _append_release_runtime_inventory(files, components)
                target = _RUNTIME_REQUIRED[-1].path
                component = next(item for item in components if item["path"] == target)
                component[field] = replacement
                with self.assertRaisesRegex(InstallerManifestError, "release runtime"):
                    _verified(files, components)

    def test_missing_desktop_entrypoint_is_rejected(self):
        files, components = _base_entries()
        _append_product_entrypoints(files, components)
        _append_release_runtime_inventory(files, components)
        files[:] = [
            item
            for item in files
            if item["target_relative_path"] != "product/AutoTrade.Desktop.exe"
        ]
        components[:] = [
            item for item in components if item["kind"] != "desktop-entrypoint"
        ]
        with self.assertRaisesRegex(InstallerManifestError, "desktop-entrypoint"):
            _verified(files, components)

    def test_missing_financial_host_entrypoint_is_rejected(self):
        files, components = _base_entries()
        _append_product_entrypoints(files, components)
        _append_release_runtime_inventory(files, components)
        files[:] = [
            item
            for item in files
            if item["target_relative_path"] != "product/AutoTrade.Host.exe"
        ]
        components[:] = [
            item for item in components if item["kind"] != "host-entrypoint"
        ]
        with self.assertRaisesRegex(InstallerManifestError, "host-entrypoint"):
            _verified(files, components)

    def test_misnamed_product_entrypoint_is_rejected(self):
        files, components = _base_entries()
        _append_product_entrypoints(files, components)
        _append_release_runtime_inventory(files, components)
        for item in components:
            if item["kind"] == "desktop-entrypoint":
                item["component_id"] = "forged-desktop-entrypoint"
        with self.assertRaisesRegex(InstallerManifestError, "noncanonical"):
            _verified(files, components)

    def test_complete_neutral_runtime_inventory_is_admitted(self):
        files, components = _base_entries()
        _append_product_entrypoints(files, components)
        _append_release_runtime_inventory(files, components)
        verified = _verified(files, components)
        paths = {item["path"] for item in verified["components"]}
        self.assertTrue({item.path for item in _RELEASE_RUNTIME_REQUIRED} <= paths)
        self.assertIn("product/AutoTrade.Desktop.exe", paths)
        self.assertIn("product/AutoTrade.Host.exe", paths)
        self.assertEqual(len(_FOUNDATION_REQUIRED), 7)
        self.assertEqual(len(_RUNTIME_REQUIRED), 30)
        self.assertEqual(len(_RELEASE_RUNTIME_REQUIRED), 37)


if __name__ == "__main__":
    unittest.main()
