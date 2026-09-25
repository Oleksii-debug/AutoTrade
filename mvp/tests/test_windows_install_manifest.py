from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from tools.build_windows_bundle import build_bundle
from tools.build_windows_install_manifest import (
    InstallerManifestError,
    build_installer_input_manifest,
    verify_release_bundle,
)


SOURCE_SHA = "a" * 40


class WindowsInstallerInputManifestTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        (self.staging / "AutoTrade.Desktop.exe").write_bytes(b"desktop")
        (self.staging / "contracts").mkdir()
        (self.staging / "contracts" / "manifest.json").write_text(
            '{"contract_version":"1.0.0"}\n',
            encoding="utf-8",
        )
        (self.staging / "dependency-lock.json").write_text(
            '{"dependencies":{"runtime":"1.0.0"}}\n',
            encoding="utf-8",
        )
        (self.staging / "sbom.spdx.json").write_text(
            '{"SPDXID":"SPDXRef-DOCUMENT","spdxVersion":"SPDX-2.3"}\n',
            encoding="utf-8",
        )
        self.provenance = self.root / "provenance.json"
        self.provenance.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "source_sha": SOURCE_SHA,
                    "release_eligible": True,
                    "blocking_issues": [],
                }
            ),
            encoding="utf-8",
        )

    def composition(self):
        components = []
        for path in sorted(self.staging.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.staging).as_posix()
            if relative == "dependency-lock.json":
                kind = "dependency-lock"
            elif relative == "sbom.spdx.json":
                kind = "sbom"
            elif relative.endswith(".exe"):
                kind = "runtime"
            else:
                kind = "asset"
            components.append(
                {
                    "component_id": relative.replace("/", "-"),
                    "kind": kind,
                    "path": relative,
                    "version": "1.0.0",
                    "sha256": "sha256:" + sha256(path.read_bytes()).hexdigest(),
                }
            )
        dependency_lock = self.staging / "dependency-lock.json"
        sbom = self.staging / "sbom.spdx.json"
        document = {
            "schema_version": "1.0.0",
            "product": "AutoTrade",
            "source_sha": SOURCE_SHA,
            "dependency_lock_sha256": "sha256:" + sha256(
                dependency_lock.read_bytes()
            ).hexdigest(),
            "sbom_sha256": "sha256:" + sha256(sbom.read_bytes()).hexdigest(),
            "schema_compatibility": {"minimum": "1.0.0", "maximum": "1.0.x"},
            "runtime": {
                "architecture": "x64",
                "runtime_identifier": "win-x64",
                "minimum_windows_version": "10.0.22621",
            },
            "components": components,
        }
        path = self.root / "composition.json"
        path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        return path

    def release_bundle(self, name="release.zip"):
        output = self.root / name
        build_bundle(
            staging=self.staging,
            output=output,
            version="1.0.0",
            source_sha=SOURCE_SHA,
            mode="release",
            provenance_path=self.provenance,
            composition_path=self.composition(),
        )
        return output

    def diagnostics_bundle(self):
        provenance = self.root / "blocked.json"
        provenance.write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "release_eligible": False,
                    "blocking_issues": [{"code": "QUALIFICATION_PENDING"}],
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "diagnostics.zip"
        build_bundle(
            staging=self.staging,
            output=output,
            version="0.1.0-dev",
            source_sha=SOURCE_SHA,
            mode="diagnostics",
            provenance_path=provenance,
        )
        return output

    def rewrite_zip(self, source, destination, mutate):
        with zipfile.ZipFile(source, "r") as original:
            entries = [(info.filename, original.read(info)) for info in original.infolist()]
        entries = mutate(entries)
        with zipfile.ZipFile(destination, "w") as archive:
            for name, payload in entries:
                archive.writestr(name, payload)

    def test_release_bundle_produces_deterministic_fail_closed_install_inventory(self):
        bundle = self.release_bundle()
        first = build_installer_input_manifest(
            bundle=bundle,
            output=self.root / "first.json",
            target_framework="net10.0-windows",
            runtime_mode="FRAMEWORK_DEPENDENT",
            runtime_prerequisite=".NET 10 Windows Desktop Runtime",
        )
        second = build_installer_input_manifest(
            bundle=bundle,
            output=self.root / "second.json",
            target_framework="net10.0-windows",
            runtime_mode="FRAMEWORK_DEPENDENT",
            runtime_prerequisite=".NET 10 Windows Desktop Runtime",
        )
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(
            (self.root / "first.json").read_bytes(),
            (self.root / "second.json").read_bytes(),
        )
        manifest = first["manifest"]
        self.assertEqual(manifest["install_scope"], "PER_USER")
        self.assertFalse(
            manifest["durable_state_policy"]["installer_may_mutate_state"]
        )
        self.assertEqual(
            manifest["durable_state_policy"]["uninstall_default"],
            "PRESERVE_DURABLE_STATE",
        )
        self.assertTrue(
            manifest["update_policy"]["verified_windows_update_plan_required"]
        )
        self.assertTrue(
            manifest["update_policy"]["blind_retry_after_unknown_state_forbidden"]
        )
        self.assertFalse(
            manifest["fresh_install_policy"][
                "trading_authority_granted_by_installer"
            ]
        )
        self.assertEqual(
            Path(first["hash_file"]).read_text(encoding="utf-8").split()[0],
            sha256((self.root / "first.json").read_bytes()).hexdigest(),
        )

    def test_diagnostics_bundle_cannot_become_installer_input(self):
        with self.assertRaisesRegex(
            InstallerManifestError,
            "release-mode",
        ):
            verify_release_bundle(self.diagnostics_bundle())

    def test_tampered_payload_digest_is_rejected(self):
        source = self.release_bundle()
        tampered = self.root / "tampered.zip"

        def mutate(entries):
            return [
                (
                    name,
                    b"tampered"
                    if name == "payload/AutoTrade.Desktop.exe"
                    else payload,
                )
                for name, payload in entries
            ]

        self.rewrite_zip(source, tampered, mutate)
        with self.assertRaisesRegex(
            InstallerManifestError,
            "digest mismatch",
        ):
            verify_release_bundle(tampered)

    def test_untracked_archive_entry_is_rejected(self):
        source = self.release_bundle()
        tampered = self.root / "extra.zip"

        def mutate(entries):
            return entries + [("surprise.txt", b"not tracked")]

        self.rewrite_zip(source, tampered, mutate)
        with self.assertRaisesRegex(
            InstallerManifestError,
            "untracked non-payload",
        ):
            verify_release_bundle(tampered)

    def test_untracked_payload_entry_is_rejected(self):
        source = self.release_bundle()
        tampered = self.root / "extra-payload.zip"

        def mutate(entries):
            return entries + [("payload/extra.dll", b"not tracked")]

        self.rewrite_zip(source, tampered, mutate)
        with self.assertRaisesRegex(
            InstallerManifestError,
            "untracked or missing payload",
        ):
            verify_release_bundle(tampered)

    def test_runtime_prerequisite_policy_is_explicit(self):
        bundle = self.release_bundle()
        with self.assertRaisesRegex(
            InstallerManifestError,
            "runtime_prerequisite",
        ):
            build_installer_input_manifest(
                bundle=bundle,
                output=self.root / "missing-runtime.json",
                target_framework="net10.0-windows",
                runtime_mode="FRAMEWORK_DEPENDENT",
            )
        with self.assertRaisesRegex(
            InstallerManifestError,
            "cannot claim external",
        ):
            build_installer_input_manifest(
                bundle=bundle,
                output=self.root / "bad-self-contained.json",
                target_framework="net10.0-windows",
                runtime_mode="SELF_CONTAINED",
                runtime_prerequisite=".NET 10 Windows Desktop Runtime",
            )

    def test_windows_case_insensitive_target_collision_is_rejected(self):
        source = self.release_bundle()
        tampered = self.root / "case-collision.zip"

        def mutate(entries):
            rewritten = []
            manifest = None
            for name, payload in entries:
                if name == "bundle-manifest.json":
                    manifest = json.loads(payload.decode("utf-8"))
                else:
                    rewritten.append((name, payload))
            self.assertIsNotNone(manifest)
            original = manifest["files"][0]
            collision_path = original["path"].swapcase()
            self.assertNotEqual(collision_path, original["path"])
            manifest["files"].append(
                {
                    "path": collision_path,
                    "sha256": original["sha256"],
                    "size": original["size"],
                }
            )
            source_payload = next(
                payload
                for name, payload in rewritten
                if name == f"payload/{original['path']}"
            )
            rewritten.append((f"payload/{collision_path}", source_payload))
            rewritten.append(
                (
                    "bundle-manifest.json",
                    json.dumps(manifest, sort_keys=True).encode("utf-8"),
                )
            )
            return rewritten

        self.rewrite_zip(source, tampered, mutate)
        with self.assertRaisesRegex(
            InstallerManifestError,
            "Windows-colliding paths",
        ):
            verify_release_bundle(tampered)

    def test_windows_separator_and_reserved_device_name_are_rejected(self):
        source = self.release_bundle()

        for unsafe_path, expected in (
            (r"contracts\\manifest.json", "unsafe for Windows"),
            ("CON.txt", "reserved Windows name"),
            ("logs./event.json", "unsafe for Windows"),
        ):
            with self.subTest(path=unsafe_path):
                tampered = self.root / (sha256(unsafe_path.encode()).hexdigest() + ".zip")

                def mutate(entries, unsafe_path=unsafe_path):
                    rewritten = []
                    manifest = None
                    old_path = None
                    old_payload = None
                    for name, payload in entries:
                        if name == "bundle-manifest.json":
                            manifest = json.loads(payload.decode("utf-8"))
                            old_path = manifest["files"][0]["path"]
                            manifest["files"][0]["path"] = unsafe_path
                        else:
                            rewritten.append((name, payload))
                    self.assertIsNotNone(manifest)
                    for index, (name, payload) in enumerate(rewritten):
                        if name == f"payload/{old_path}":
                            old_payload = payload
                            rewritten[index] = (f"payload/{unsafe_path}", payload)
                            break
                    self.assertIsNotNone(old_payload)
                    rewritten.append(
                        (
                            "bundle-manifest.json",
                            json.dumps(manifest, sort_keys=True).encode("utf-8"),
                        )
                    )
                    return rewritten

                self.rewrite_zip(source, tampered, mutate)
                with self.assertRaisesRegex(InstallerManifestError, expected):
                    verify_release_bundle(tampered)

    def test_installer_manifest_retains_exact_release_composition_identity(self):
        bundle = self.release_bundle()
        built = build_installer_input_manifest(
            bundle=bundle,
            output=self.root / "composition-bound.json",
            target_framework="net10.0-windows",
            runtime_mode="FRAMEWORK_DEPENDENT",
            runtime_prerequisite=".NET 10 Windows Desktop Runtime",
        )
        manifest = built["manifest"]
        composition = self.composition()
        raw = json.loads(composition.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["dependency_lock_sha256"],
            raw["dependency_lock_sha256"],
        )
        self.assertEqual(manifest["sbom_sha256"], raw["sbom_sha256"])
        self.assertEqual(
            manifest["schema_compatibility"],
            raw["schema_compatibility"],
        )
        self.assertEqual(manifest["platform"], raw["runtime"])
        self.assertEqual(
            {item["path"]: item["sha256"] for item in manifest["components"]},
            {item["path"]: item["sha256"] for item in raw["components"]},
        )
        self.assertTrue(manifest["composition_sha256"].startswith("sha256:"))

    def test_tampered_composition_dependency_or_sbom_binding_is_rejected(self):
        source = self.release_bundle()
        for field, kind in (
            ("dependency_lock_sha256", "dependency-lock"),
            ("sbom_sha256", "sbom"),
        ):
            with self.subTest(field=field):
                tampered = self.root / f"tampered-{field}.zip"

                def mutate(entries, field=field):
                    result = []
                    for name, payload in entries:
                        if name != "bundle-manifest.json":
                            result.append((name, payload))
                            continue
                        manifest = json.loads(payload)
                        manifest["composition"][field] = "sha256:" + "f" * 64
                        result.append(
                            (
                                name,
                                json.dumps(manifest, sort_keys=True).encode("utf-8"),
                            )
                        )
                    return result

                self.rewrite_zip(source, tampered, mutate)
                with self.assertRaisesRegex(
                    InstallerManifestError,
                    f"composition {kind} identity",
                ):
                    verify_release_bundle(tampered)

    def test_tampered_composition_runtime_identity_is_rejected(self):
        source = self.release_bundle()
        cases = (
            ("rid", "win-arm64", "runtime_identifier"),
            ("architecture", "mips64", "architecture"),
            ("minimum", "Windows 11", "major.minor.build"),
        )
        for name, value, pattern in cases:
            with self.subTest(name=name):
                tampered = self.root / f"runtime-{name}.zip"

                def mutate(entries, name=name, value=value):
                    result = []
                    for entry, payload in entries:
                        if entry != "bundle-manifest.json":
                            result.append((entry, payload))
                            continue
                        manifest = json.loads(payload)
                        if name == "rid":
                            manifest["composition"]["runtime"]["runtime_identifier"] = value
                        elif name == "architecture":
                            manifest["composition"]["runtime"]["architecture"] = value
                        else:
                            manifest["composition"]["runtime"]["minimum_windows_version"] = value
                        result.append(
                            (
                                entry,
                                json.dumps(manifest, sort_keys=True).encode("utf-8"),
                            )
                        )
                    return result

                self.rewrite_zip(source, tampered, mutate)
                with self.assertRaisesRegex(InstallerManifestError, pattern):
                    verify_release_bundle(tampered)

    def test_composition_component_inventory_must_equal_verified_payload(self):
        source = self.release_bundle()
        tampered = self.root / "component-inventory-mismatch.zip"

        def mutate(entries):
            result = []
            for name, payload in entries:
                if name != "bundle-manifest.json":
                    result.append((name, payload))
                    continue
                manifest = json.loads(payload)
                manifest["composition"]["components"] = manifest["composition"]["components"][:-1]
                result.append(
                    (
                        name,
                        json.dumps(manifest, sort_keys=True).encode("utf-8"),
                    )
                )
            return result

        self.rewrite_zip(source, tampered, mutate)
        with self.assertRaisesRegex(
            InstallerManifestError,
            "component inventory does not match",
        ):
            verify_release_bundle(tampered)

    def test_composition_unknown_fields_fail_closed_at_installer_boundary(self):
        source = self.release_bundle()
        tampered = self.root / "composition-extra-field.zip"

        def mutate(entries):
            result = []
            for name, payload in entries:
                if name != "bundle-manifest.json":
                    result.append((name, payload))
                    continue
                manifest = json.loads(payload)
                manifest["composition"]["installer_hint"] = "trust-me"
                result.append(
                    (
                        name,
                        json.dumps(manifest, sort_keys=True).encode("utf-8"),
                    )
                )
            return result

        self.rewrite_zip(source, tampered, mutate)
        with self.assertRaisesRegex(
            InstallerManifestError,
            "composition structure is not canonical",
        ):
            verify_release_bundle(tampered)

    def test_manifest_inventory_binds_exact_bundle_bytes(self):
        bundle = self.release_bundle()
        verified = verify_release_bundle(bundle)
        self.assertEqual(
            verified["bundle_sha256"],
            "sha256:" + sha256(bundle.read_bytes()).hexdigest(),
        )
        paths = [item["target_relative_path"] for item in verified["files"]]
        self.assertEqual(
            paths,
            [
                "AutoTrade.Desktop.exe",
                "contracts/manifest.json",
                "dependency-lock.json",
                "sbom.spdx.json",
            ],
        )


    def tamper_manifest_path(self, source, destination, new_path):
        with zipfile.ZipFile(source, "r") as original:
            manifest = json.loads(original.read("bundle-manifest.json"))
            original_path = manifest["files"][0]["path"]
            original_payload = original.read(f"payload/{original_path}")
            other_entries = [
                (info.filename, original.read(info))
                for info in original.infolist()
                if info.filename not in {
                    "bundle-manifest.json",
                    f"payload/{original_path}",
                }
            ]
        manifest["files"][0]["path"] = new_path
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr(
                "bundle-manifest.json",
                json.dumps(manifest).encode("utf-8"),
            )
            archive.writestr(f"payload/{new_path}", original_payload)
            for name, payload in other_entries:
                archive.writestr(name, payload)

    def test_windows_path_confusion_is_rejected(self):
        source = self.release_bundle()
        cases = (
            ("backslash.zip", r"dir\\evil.exe", "Windows separator"),
            ("drive.zip", "C:evil.exe", "Windows-forbidden"),
            ("reserved.zip", "CON.txt", "reserved Windows device"),
            ("trailing.zip", "evil. ", "trailing"),
            ("wildcard.zip", "bad?.dll", "Windows-forbidden"),
            ("noncanonical.zip", "dir//file.dll", "unsafe"),
        )
        for name, path, pattern in cases:
            with self.subTest(path=path):
                tampered = self.root / name
                self.tamper_manifest_path(source, tampered, path)
                with self.assertRaisesRegex(
                    InstallerManifestError,
                    pattern,
                ):
                    verify_release_bundle(tampered)

    def test_unknown_or_future_bundle_manifest_shape_fails_closed(self):
        source = self.release_bundle()
        for mode in ("unknown-field", "future-schema"):
            with self.subTest(mode=mode):
                tampered = self.root / f"{mode}.zip"

                def mutate(entries):
                    result = []
                    for name, payload in entries:
                        if name != "bundle-manifest.json":
                            result.append((name, payload))
                            continue
                        manifest = json.loads(payload)
                        if mode == "unknown-field":
                            manifest["surprise"] = True
                        else:
                            manifest["schema_version"] = "2.0.0"
                        result.append(
                            (
                                name,
                                json.dumps(manifest).encode("utf-8"),
                            )
                        )
                    return result

                self.rewrite_zip(source, tampered, mutate)
                expected = (
                    "fields do not match"
                    if mode == "unknown-field"
                    else "unsupported bundle manifest schema"
                )
                with self.assertRaisesRegex(
                    InstallerManifestError,
                    expected,
                ):
                    verify_release_bundle(tampered)


if __name__ == "__main__":
    unittest.main()
