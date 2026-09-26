from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.build_windows_bundle import build_bundle
from tools.build_windows_install_manifest import build_installer_input_manifest
import tools.build_windows_velopack as velopack_module
from tools.build_windows_velopack import (
    VelopackPackagingError,
    build_velopack_release,
)


SOURCE_SHA = "a" * 40


class WindowsVelopackPackagingTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        (self.staging / "AutoTrade.Desktop.exe").write_bytes(b"desktop")
        (self.staging / "AutoTrade.Desktop.dll").write_bytes(b"managed")
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
            elif relative.endswith((".exe", ".dll")):
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

    def release_inputs(
        self,
        *,
        version="1.2.3",
        runtime_mode="SELF_CONTAINED",
        runtime_prerequisite=None,
        stem="release",
    ):
        bundle = self.root / f"{stem}.zip"
        build_bundle(
            staging=self.staging,
            output=bundle,
            version=version,
            source_sha=SOURCE_SHA,
            mode="release",
            provenance_path=self.provenance,
            composition_path=self.composition(),
        )
        manifest = self.root / f"{stem}-installer.json"
        build_installer_input_manifest(
            bundle=bundle,
            output=manifest,
            target_framework="net10.0-windows",
            runtime_mode=runtime_mode,
            runtime_prerequisite=runtime_prerequisite,
        )
        return bundle, manifest

    def successful_runner(self, captured, *, add_portable=False):
        def run(command, **kwargs):
            captured["command"] = list(command)
            captured["kwargs"] = kwargs
            pack_dir = Path(command[command.index("--packDir") + 1])
            output_dir = Path(command[command.index("--outputDir") + 1])
            version = command[command.index("--packVersion") + 1]
            self.assertEqual((pack_dir / "AutoTrade.Desktop.exe").read_bytes(), b"desktop")
            self.assertEqual((pack_dir / "AutoTrade.Desktop.dll").read_bytes(), b"managed")
            self.assertEqual(
                {path.relative_to(pack_dir).as_posix() for path in pack_dir.rglob("*") if path.is_file()},
                {
                    "AutoTrade.Desktop.exe",
                    "AutoTrade.Desktop.dll",
                    "dependency-lock.json",
                    "sbom.spdx.json",
                },
            )
            (output_dir / "AutoTrade-Setup.exe").write_bytes(b"setup")
            (output_dir / f"AutoTrade-{version}-full.nupkg").write_bytes(b"package")
            (output_dir / "releases.win.json").write_text(
                '{"channel":"win"}\n',
                encoding="utf-8",
            )
            (output_dir / "assets.win.json").write_text(
                '{"assets":[]}\n',
                encoding="utf-8",
            )
            (output_dir / "RELEASES").write_text("", encoding="utf-8")
            if add_portable:
                (output_dir / "AutoTrade-Portable.zip").write_bytes(b"portable")
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        return run

    def test_build_uses_exact_verified_payload_and_sanitized_pinned_vpk(self):
        bundle, manifest = self.release_inputs()
        output = self.root / "out"
        captured = {}

        with patch.dict(
            os.environ,
            {
                "VPK_SIGN_PARAMS": "secret-that-must-not-reach-vpk",
                "VPK_PACK_ID": "WrongProduct",
                "VPK_NO_PORTABLE": "false",
            },
            clear=False,
        ):
            result = build_velopack_release(
                bundle=bundle,
                installer_manifest=manifest,
                output_dir=output,
                runner=self.successful_runner(captured),
            )

        command = captured["command"]
        self.assertEqual(
            command[:10],
            [
                "dotnet",
                "tool",
                "run",
                "vpk",
                "--",
                "--skip-updates",
                "true",
                "--yes",
                "true",
                "pack",
            ],
        )
        self.assertEqual(command[command.index("--packId") + 1], "AutoTrade")
        self.assertEqual(command[command.index("--packVersion") + 1], "1.2.3")
        self.assertEqual(command[command.index("--mainExe") + 1], "AutoTrade.Desktop.exe")
        self.assertEqual(command[command.index("--runtime") + 1], "win-x64")
        self.assertEqual(command[command.index("--instLocation") + 1], "PerUser")
        self.assertEqual(command[command.index("--noPortable") + 1], "true")
        self.assertEqual(command[command.index("--exclude") + 1], "(?!)")
        self.assertEqual(command[command.index("--noDefaultExclude") + 1], "true")
        self.assertNotIn("--msi", command)
        self.assertNotIn("--signParams", command)
        self.assertFalse(
            any(key.upper().startswith("VPK_") for key in captured["kwargs"]["env"])
        )
        self.assertEqual(captured["kwargs"]["cwd"], velopack_module.ROOT)
        self.assertFalse(captured["kwargs"]["check"])
        self.assertEqual(captured["kwargs"]["timeout"], 900)

        build_manifest = result["manifest"]
        self.assertEqual(build_manifest["source_sha"], SOURCE_SHA)
        self.assertEqual(build_manifest["version"], "1.2.3")
        self.assertEqual(
            build_manifest["installer_technology"],
            {
                "name": "Velopack",
                "version": "1.2.158",
                "pack_id": "AutoTrade",
                "delivery_mode": "PER_USER_SETUP",
            },
        )
        self.assertEqual(build_manifest["signing_status"], "UNSIGNED_REQUIRES_WP64")
        self.assertFalse(build_manifest["release_eligible"])
        self.assertFalse(build_manifest["trading_authority_granted_by_artifact"])
        names = {item["path"] for item in build_manifest["artifacts"]}
        self.assertEqual(
            names,
            {
                "AutoTrade-Setup.exe",
                "AutoTrade-1.2.3-full.nupkg",
                "releases.win.json",
                "assets.win.json",
                "RELEASES",
            },
        )
        self.assertTrue((output / "autotrade-velopack-build.json").is_file())
        self.assertTrue((output / "autotrade-velopack-build.json.sha256").is_file())
        for name in names:
            self.assertTrue((output / name).is_file())
            self.assertTrue((output / f"{name}.sha256").is_file())
        self.assertFalse(any("portable" in path.name.casefold() for path in output.iterdir()))

    def test_framework_dependent_manifest_maps_only_declared_prerequisite(self):
        bundle, manifest = self.release_inputs(
            runtime_mode="FRAMEWORK_DEPENDENT",
            runtime_prerequisite="net10-x64-desktop",
            stem="framework",
        )
        captured = {}
        build_velopack_release(
            bundle=bundle,
            installer_manifest=manifest,
            output_dir=self.root / "framework-out",
            runner=self.successful_runner(captured),
        )
        command = captured["command"]
        self.assertEqual(
            command[command.index("--framework") + 1],
            "net10-x64-desktop",
        )

    def test_manifest_digest_tamper_fails_before_vpk_execution(self):
        bundle, manifest = self.release_inputs(stem="tampered-sidecar")
        manifest.with_suffix(manifest.suffix + ".sha256").write_text(
            f"{'0' * 64}  {manifest.name}\n",
            encoding="utf-8",
        )
        called = False

        def forbidden_runner(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("runner must not be called")

        with self.assertRaisesRegex(
            VelopackPackagingError,
            "digest does not match",
        ):
            build_velopack_release(
                bundle=bundle,
                installer_manifest=manifest,
                output_dir=self.root / "tampered-sidecar-out",
                runner=forbidden_runner,
            )
        self.assertFalse(called)

    def test_installer_manifest_from_other_bundle_fails_before_vpk(self):
        first_bundle, first_manifest = self.release_inputs(
            version="1.2.3",
            stem="first",
        )
        second_bundle, _ = self.release_inputs(
            version="1.2.4",
            stem="second",
        )
        del first_bundle

        with self.assertRaisesRegex(
            VelopackPackagingError,
            "does not match verified release bundle",
        ):
            build_velopack_release(
                bundle=second_bundle,
                installer_manifest=first_manifest,
                output_dir=self.root / "mismatch-out",
                runner=lambda *args, **kwargs: self.fail("vpk must not run"),
            )

    def test_portable_output_is_rejected_before_final_publication(self):
        bundle, manifest = self.release_inputs(stem="portable")
        output = self.root / "portable-out"
        with self.assertRaisesRegex(
            VelopackPackagingError,
            "portable Velopack output is forbidden",
        ):
            build_velopack_release(
                bundle=bundle,
                installer_manifest=manifest,
                output_dir=output,
                runner=self.successful_runner({}, add_portable=True),
            )
        self.assertEqual(list(output.iterdir()), [])

    def test_substring_package_version_is_rejected_before_publication(self):
        bundle, manifest = self.release_inputs(version="1.2.3", stem="version-substring")
        output = self.root / "version-substring-out"

        def wrong_version_runner(command, **kwargs):
            output_dir = Path(command[command.index("--outputDir") + 1])
            (output_dir / "AutoTrade-Setup.exe").write_bytes(b"setup")
            (output_dir / "AutoTrade-11.2.30-full.nupkg").write_bytes(b"package")
            (output_dir / "releases.win.json").write_text(
                '{"channel":"win"}\n',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        with self.assertRaisesRegex(
            VelopackPackagingError,
            "does not exactly bind the requested version",
        ):
            build_velopack_release(
                bundle=bundle,
                installer_manifest=manifest,
                output_dir=output,
                runner=wrong_version_runner,
            )

        self.assertEqual(list(output.iterdir()), [])

    def test_nonzero_vpk_exit_does_not_publish_partial_release(self):
        bundle, manifest = self.release_inputs(stem="failure")
        output = self.root / "failure-out"

        def failed_runner(command, **kwargs):
            Path(command[command.index("--outputDir") + 1], "partial.bin").write_bytes(
                b"partial"
            )
            return subprocess.CompletedProcess(
                command,
                17,
                stdout="secret-looking stdout",
                stderr="secret-looking stderr",
            )

        with self.assertRaisesRegex(
            VelopackPackagingError,
            "exit code 17",
        ) as context:
            build_velopack_release(
                bundle=bundle,
                installer_manifest=manifest,
                output_dir=output,
                runner=failed_runner,
            )
        self.assertNotIn("secret-looking", str(context.exception))
        self.assertEqual(list(output.iterdir()), [])

    def test_output_path_swap_after_validation_fails_before_publication(self):
        bundle, manifest = self.release_inputs(stem="output-swap")
        output = self.root / "output-swap-out"
        original_validate = velopack_module._validate_vpk_outputs

        def validate_then_swap(directory, *, version):
            snapshots = original_validate(directory, version=version)
            victim = next(
                path
                for path, _digest, _size in snapshots
                if path.name == "AutoTrade-Setup.exe"
            )
            victim.unlink()
            victim.write_bytes(b"evil!")
            return snapshots

        with patch.object(
            velopack_module,
            "_validate_vpk_outputs",
            side_effect=validate_then_swap,
        ):
            with self.assertRaisesRegex(
                VelopackPackagingError,
                "changed after validation",
            ):
                build_velopack_release(
                    bundle=bundle,
                    installer_manifest=manifest,
                    output_dir=output,
                    runner=self.successful_runner({}),
                )

        self.assertEqual(list(output.iterdir()), [])

    def test_output_same_inode_mutation_after_validation_fails_before_publication(self):
        bundle, manifest = self.release_inputs(stem="output-mutation")
        output = self.root / "output-mutation-out"
        original_validate = velopack_module._validate_vpk_outputs

        def validate_then_mutate(directory, *, version):
            snapshots = original_validate(directory, version=version)
            victim = next(
                path
                for path, _digest, _size in snapshots
                if path.name == "AutoTrade-Setup.exe"
            )
            with victim.open("r+b") as handle:
                handle.seek(0)
                handle.write(b"evil!")
                handle.flush()
                os.fsync(handle.fileno())
            return snapshots

        with patch.object(
            velopack_module,
            "_validate_vpk_outputs",
            side_effect=validate_then_mutate,
        ):
            with self.assertRaisesRegex(
                VelopackPackagingError,
                "changed after validation",
            ):
                build_velopack_release(
                    bundle=bundle,
                    installer_manifest=manifest,
                    output_dir=output,
                    runner=self.successful_runner({}),
                )

        self.assertEqual(list(output.iterdir()), [])

    def test_existing_output_content_fails_before_any_vpk_execution(self):
        bundle, manifest = self.release_inputs(stem="existing")
        output = self.root / "existing-out"
        output.mkdir()
        marker = output / "do-not-overwrite.txt"
        marker.write_text("preserve", encoding="utf-8")
        called = False

        def forbidden_runner(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("runner must not be called")

        with self.assertRaisesRegex(
            VelopackPackagingError,
            "must be empty",
        ):
            build_velopack_release(
                bundle=bundle,
                installer_manifest=manifest,
                output_dir=output,
                runner=forbidden_runner,
            )
        self.assertFalse(called)
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
