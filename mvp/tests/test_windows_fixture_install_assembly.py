"""Section-6 nonexecuting fixture install/update/uninstall/reinstall evidence.

Reuses the EXISTING reviewed release bundle fixture and canonical verifier.
No Windows service, Host or installer binary ever executes in these scenarios.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import unittest

from mvp.tests.test_windows_install_manifest import (
    WindowsInstallerInputManifestTests as CanonicalReleaseBundleFixtures,
)
from tools.windows_fixture_install_assembly import (
    FixtureAssemblyError,
    fixture_install_bundle,
    fixture_rollback_version,
    fixture_uninstall,
)


class WindowsFixtureInstallAssemblyTests(unittest.TestCase):
    def setUp(self):
        # Use canonical release-bundle test setup; do not add a second bundler
        # or invent alternative provenance fields just for fixture tests.
        self.canonical = CanonicalReleaseBundleFixtures(
            "test_release_bundle_produces_deterministic_fail_closed_install_inventory"
        )
        self.canonical.setUp()
        self.addCleanup(self.canonical.doCleanups)
        self.root = self.canonical.root / "autotrade-fixture-only"
        self.bundle = self.canonical.release_bundle()
        self.state = self.root / "state"
        self.state.mkdir(parents=True)
        self.data = self.state / "journal-do-not-mutate.json"
        self.data.write_text('{"durable":"keep-original"}\n', encoding="utf-8")
        self.data_original = self.data.read_bytes()

    def install(self, bundle=None, *, fail_after_files=None):
        return fixture_install_bundle(
            bundle or self.bundle,
            self.root,
            fixture_only=True,
            fail_after_files=fail_after_files,
        )

    def new_version(self):
        (self.canonical.staging / "AutoTrade.Desktop.exe").write_bytes(b"desktop-fixture-next")
        return self.canonical.release_bundle("next-release.zip")

    def selected(self):
        return json.loads((self.root / "selected-fixture.json").read_text(encoding="utf-8"))

    def test_clean_install_binds_verified_source_archive_and_no_host_authority(self):
        result = self.install()
        self.assertEqual(result["disposition"], "FIXTURE_STAGED_AND_SELECTED")
        self.assertFalse(result["host_started"])
        self.assertFalse(result["trading_authority_granted"])
        self.assertFalse(result["release_qualified"])
        directory = self.root / "versions" / result["version_directory"]
        self.assertEqual(
            (directory / "payload/AutoTrade.Desktop.exe").read_bytes(), b"desktop"
        )
        self.assertEqual(self.selected()["version_directory"], result["version_directory"])
        self.assertEqual(self.data.read_bytes(), self.data_original)

    def test_update_is_side_by_side_and_preserves_old_install_and_user_state(self):
        old = self.install()
        new = self.install(self.new_version())
        self.assertNotEqual(old["version_directory"], new["version_directory"])
        self.assertEqual(new["replaced_version"], old["version_directory"])
        self.assertTrue((self.root / "versions" / old["version_directory"]).is_dir())
        self.assertEqual(self.selected()["version_directory"], new["version_directory"])
        self.assertEqual(self.data.read_bytes(), self.data_original)

    def test_explicit_rollback_reselects_verified_previous_version_without_effects(self):
        old = self.install()
        self.install(self.new_version())
        decision = fixture_rollback_version(
            self.root, old["version_directory"], fixture_only=True
        )
        self.assertEqual(decision["disposition"], "FIXTURE_VERSION_SELECTION_ONLY")
        self.assertEqual(self.selected()["version_directory"], old["version_directory"])
        self.assertFalse(decision["host_started"])
        self.assertEqual(self.data.read_bytes(), self.data_original)

    def test_uninstall_and_reinstall_preserve_durable_data(self):
        first = self.install()
        result = fixture_uninstall(self.root, fixture_only=True)
        self.assertTrue(result["durable_state_preserved"])
        self.assertFalse((self.root / "selected-fixture.json").exists())
        self.assertFalse((self.root / "versions" / first["version_directory"]).exists())
        self.assertEqual(self.data.read_bytes(), self.data_original)
        again = self.install()
        self.assertEqual(again["version_directory"], first["version_directory"])
        self.assertEqual(self.data.read_bytes(), self.data_original)

    def test_duplicate_archive_cannot_overwrite_verified_installed_version(self):
        first = self.install()
        initial = self.selected()
        with self.assertRaisesRegex(FixtureAssemblyError, "already installed"):
            self.install()
        self.assertEqual(self.selected(), initial)
        self.assertTrue((self.root / "versions" / first["version_directory"]).exists())

    def test_diagnostics_archive_is_not_mistaken_for_eligible_release_fixture(self):
        archive = self.canonical.diagnostics_bundle()
        with self.assertRaisesRegex(Exception, "release-mode bundle"):
            self.install(archive)
        self.assertFalse((self.root / "selected-fixture.json").exists())

    def test_corrupted_bundle_rejected_without_mutating_prior_selection(self):
        old = self.install()
        new = self.new_version()
        with new.open("r+b") as handle:
            handle.seek(50)
            b = handle.read(1)
            handle.seek(50)
            handle.write(bytes([b[0] ^ 0xFF]))
        with self.assertRaises(Exception):
            self.install(new)
        self.assertEqual(self.selected()["version_directory"], old["version_directory"])
        self.assertEqual(self.data.read_bytes(), self.data_original)

    def test_disk_full_mid_extraction_never_selects_partial_candidate(self):
        old = self.install()
        with self.assertRaisesRegex(OSError, "simulated staging disk full"):
            self.install(self.new_version(), fail_after_files=1)
        self.assertEqual(self.selected()["version_directory"], old["version_directory"])
        self.assertFalse(any(
            p.name.startswith(".stage-") for p in (self.root / "versions").iterdir()
        ))
        self.assertEqual(self.data.read_bytes(), self.data_original)

    def test_disk_full_on_clean_install_does_not_create_selection(self):
        with self.assertRaisesRegex(OSError, "simulated staging disk full"):
            self.install(fail_after_files=0)
        self.assertFalse((self.root / "selected-fixture.json").exists())
        self.assertEqual(self.data.read_bytes(), self.data_original)

    def test_changed_installed_payload_blocks_rollback(self):
        first = self.install()
        self.install(self.new_version())
        (self.root / "versions" / first["version_directory"] /
         "payload/AutoTrade.Desktop.exe").write_bytes(b"mutated")
        with self.assertRaisesRegex(FixtureAssemblyError, "differs from verified"):
            fixture_rollback_version(
                self.root, first["version_directory"], fixture_only=True
            )
        self.assertNotEqual(self.selected()["version_directory"], first["version_directory"])

    def test_unknown_rollback_version_is_rejected(self):
        self.install()
        with self.assertRaises(FixtureAssemblyError):
            fixture_rollback_version(self.root, "a"*40+"-"+"b"*64, fixture_only=True)

    def test_tampered_selected_record_cannot_trigger_uninstall(self):
        self.install()
        selection = self.root / "selected-fixture.json"
        value = self.selected()
        value["trading_authority_granted"] = True
        selection.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(FixtureAssemblyError, "selected version record is invalid"):
            fixture_uninstall(self.root, fixture_only=True)
        self.assertEqual(self.data.read_bytes(), self.data_original)

    def test_symlinked_versions_directory_rejected_without_touching_external_data(self):
        other = self.canonical.root / "other"
        other.mkdir()
        (other / "leave-alone.txt").write_text("private")
        folder = self.root / "versions"
        try:
            folder.symlink_to(other, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink privilege unavailable")
        with self.assertRaisesRegex(FixtureAssemblyError, "symlink"):
            self.install()
        self.assertEqual((other / "leave-alone.txt").read_text(), "private")

    def test_hardlinked_installed_payload_blocks_uninstall(self):
        result = self.install()
        target = self.root / "versions" / result["version_directory"] / "payload/AutoTrade.Desktop.exe"
        try:
            os.link(target, self.canonical.root / "alias.exe")
        except (OSError, NotImplementedError):
            self.skipTest("hardlinks unavailable")
        with self.assertRaisesRegex(FixtureAssemblyError, "not regular"):
            fixture_uninstall(self.root, fixture_only=True)
        self.assertEqual(self.data.read_bytes(), self.data_original)

    def test_fixture_marker_and_absolute_path_are_required(self):
        with self.assertRaisesRegex(FixtureAssemblyError, "fixture_only"):
            fixture_install_bundle(self.bundle, self.root, fixture_only=False)
        with self.assertRaisesRegex(FixtureAssemblyError, "absolute"):
            fixture_install_bundle(
                self.bundle, Path("autotrade-fixture-only"), fixture_only=True
            )
        with self.assertRaisesRegex(FixtureAssemblyError, "autotrade-fixture-only"):
            fixture_install_bundle(self.bundle, self.canonical.root / "AutoTrade",
                                   fixture_only=True)

    def test_two_concurrent_installers_cannot_both_select_same_version(self):
        def try_install(_):
            try:
                return self.install()["disposition"]
            except FixtureAssemblyError as error:
                if "already installed" not in str(error):
                    raise
                return "DENIED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(try_install, range(2)))
        self.assertEqual(outcomes, ["DENIED", "FIXTURE_STAGED_AND_SELECTED"])

    def test_no_unverified_extra_program_file_allowed(self):
        result = self.install()
        extra = self.root / "versions" / result["version_directory"] / "payload/extra-secret"
        extra.write_text("not inventoried")
        with self.assertRaisesRegex(FixtureAssemblyError, "untracked"):
            fixture_uninstall(self.root, fixture_only=True)
        self.assertEqual(self.data.read_bytes(), self.data_original)


if __name__ == "__main__":
    unittest.main()
