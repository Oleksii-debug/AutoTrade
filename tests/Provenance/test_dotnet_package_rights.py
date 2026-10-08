from __future__ import annotations

import base64
from hashlib import sha512
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from tools.dotnet_package_rights import (
    ROOT,
    package_rights_blockers,
    verify_restored_package_rights,
)


_LICENSE = """Copyright (C) Example Corporation. All rights reserved.

Redistribution in binary form is permitted when this notice is retained.
"""
_NOTICE = "required notice\n"
_NUSPEC = (
    "<?xml version=\"1.0\"?>\n"
    "<package><metadata><id>Example.Package</id><version>1.2.3</version>"
    "<license type=\"file\">LICENSE.txt</license></metadata></package>\n"
)


def _canonical_nupkg_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in (
            ("LICENSE.txt", _LICENSE.encode("utf-8")),
            ("NOTICE.txt", _NOTICE.encode("utf-8")),
            ("example.package.nuspec", _NUSPEC.encode("utf-8")),
        ):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


_NUPKG_BYTES = _canonical_nupkg_bytes()
_HASH = base64.b64encode(sha512(_NUPKG_BYTES).digest()).decode("ascii")


def _write_project(root: Path) -> Path:
    project = root / "src" / "App" / "App.csproj"
    project.parent.mkdir(parents=True)
    project.write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk\">\n"
        "  <PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup>\n"
        "  <ItemGroup><PackageReference Include=\"Example.Package\" Version=\"1.2.3\" /></ItemGroup>\n"
        "</Project>\n",
        encoding="utf-8",
    )
    (project.parent / "packages.lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "dependencies": {
                    "net10.0": {
                        "Example.Package": {
                            "type": "Direct",
                            "requested": "[1.2.3, )",
                            "resolved": "1.2.3",
                            "contentHash": _HASH,
                        }
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return project


def _write_rights_workflow(root: Path, projects: list[Path]) -> None:
    workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "env:",
        "  NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages",
        "jobs:",
        "  verify:",
        "    steps:",
    ]
    for project in projects:
        relative = project.relative_to(root).as_posix()
        lines.append(f"      - run: dotnet restore {relative} --locked-mode")
        lines.append(
            "      - run: python tools/dotnet_package_rights.py "
            "--verify-restored "
            '--packages-root "${{ env.NUGET_PACKAGES }}" '
            f"--project {relative}"
        )
    workflow.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_policy(root: Path) -> None:
    licenses = root / "provenance" / "licenses"
    licenses.mkdir(parents=True)
    (licenses / "Example.Package.LICENSE.txt").write_text(_LICENSE, encoding="utf-8")
    (root / "provenance" / "dotnet-package-rights.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "packages": [
                    {
                        "name": "Example.Package",
                        "version": "1.2.3",
                        "content_hash_sha512_base64": _HASH,
                        "license_id": "EXAMPLE-REDISTRIBUTABLE",
                        "license_file": "LICENSE.txt",
                        "expected_license_text_path": "provenance/licenses/Example.Package.LICENSE.txt",
                        "notice_file": "NOTICE.txt",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_restored_package(root: Path, *, license_text: str = _LICENSE) -> Path:
    packages = root / "packages"
    package = packages / "example.package" / "1.2.3"
    package.mkdir(parents=True)
    (package / "example.package.1.2.3.nupkg.sha512").write_text(
        _HASH,
        encoding="ascii",
    )
    (package / "example.package.1.2.3.nupkg").write_bytes(_NUPKG_BYTES)
    (package / "LICENSE.txt").write_text(license_text, encoding="utf-8")
    (package / "NOTICE.txt").write_text(_NOTICE, encoding="utf-8")
    (package / "example.package.nuspec").write_text(_NUSPEC, encoding="utf-8")
    return packages


class DotnetPackageRightsTests(unittest.TestCase):
    def test_repository_locked_graph_has_exact_rights_coverage(self):
        self.assertEqual(package_rights_blockers(ROOT), [])

    def test_desktop_locked_win_x64_restore_requires_exact_rid_and_order(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            generic_project = _write_project(root)
            desktop_dir = root / "src" / "AutoTrade.Desktop"
            generic_project.parent.rename(desktop_dir)
            project = desktop_dir / "AutoTrade.Desktop.csproj"
            (desktop_dir / "App.csproj").rename(project)
            _write_policy(root)
            _write_rights_workflow(root, [project])
            workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
            original = workflow.read_text(encoding="utf-8")
            relative = project.relative_to(root).as_posix()
            exact = f"dotnet restore {relative} --locked-mode"
            windows = f"dotnet restore {relative} -r win-x64 --locked-mode"
            self.assertIn(exact, original)
            workflow.write_text(original.replace(exact, windows), encoding="utf-8")
            self.assertEqual(package_rights_blockers(root), [])
            for invalid in (
                f"dotnet restore {relative} -r linux-x64 --locked-mode",
                f"dotnet restore {relative} -r win-x64",
            ):
                with self.subTest(invalid=invalid):
                    workflow.write_text(
                        original.replace(exact, invalid), encoding="utf-8"
                    )
                    self.assertIn(
                        f"DOTNET_PACKAGE_RIGHTS_VERIFY_ORDER_INVALID:{relative}",
                        package_rights_blockers(root),
                    )

    def test_missing_rights_record_blocks_locked_package(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_rights_workflow(root, [project])
            (root / "provenance").mkdir()
            (root / "provenance" / "dotnet-package-rights.json").write_text(
                '{"schema_version":"1.0.0","packages":[]}\n',
                encoding="utf-8",
            )
            self.assertEqual(
                package_rights_blockers(root),
                ["DOTNET_PACKAGE_RIGHTS_MISSING:Example.Package@1.2.3"],
            )

    def test_package_project_without_post_restore_rights_verification_is_blocked(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "env:\n"
                "  NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages\n",
                encoding="utf-8",
            )
            self.assertIn(
                "DOTNET_PACKAGE_RIGHTS_VERIFY_PROJECT_MISSING:"
                "src/App/App.csproj",
                package_rights_blockers(root),
            )

    def test_nuget_packages_authority_override_is_blocked(self):
        for override in (
            '      env: {NUGET_PACKAGES: /tmp/alternate}\n',
            '      - run: echo "NUGET_PACKAGES=/tmp/alternate" >> "$GITHUB_ENV"\n',
        ):
            with self.subTest(override=override.strip()):
                with TemporaryDirectory() as directory:
                    root = Path(directory)
                    project = _write_project(root)
                    _write_policy(root)
                    workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
                    workflow.parent.mkdir(parents=True)
                    workflow.write_text(
                        "env:\n"
                        "  NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages\n"
                        "jobs:\n"
                        "  verify:\n"
                        "    steps:\n"
                        + override
                        + "      - run: dotnet restore src/App/App.csproj --locked-mode\n"
                        "      - run: python tools/dotnet_package_rights.py "
                        "--verify-restored "
                        '--packages-root "${{ env.NUGET_PACKAGES }}" '
                        "--project src/App/App.csproj\n",
                        encoding="utf-8",
                    )
                    self.assertIn(
                        "DOTNET_PACKAGE_RIGHTS_NUGET_PACKAGES_AUTHORITY_INVALID",
                        package_rights_blockers(root),
                    )

    def test_rights_verifier_must_follow_exact_locked_restore(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "env:\n"
                "  NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages\n"
                "jobs:\n"
                "  verify:\n"
                "    steps:\n"
                "      - run: python tools/dotnet_package_rights.py "
                "--verify-restored "
                '--packages-root "${{ env.NUGET_PACKAGES }}" '
                "--project src/App/App.csproj\n"
                "      - run: dotnet restore src/App/App.csproj --locked-mode\n",
                encoding="utf-8",
            )
            self.assertIn(
                "DOTNET_PACKAGE_RIGHTS_VERIFY_ORDER_INVALID:src/App/App.csproj",
                package_rights_blockers(root),
            )

    def test_restore_and_rights_verifier_must_share_one_job(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "env:\n"
                "  NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages\n"
                "jobs:\n"
                "  restore:\n"
                "    steps:\n"
                "      - run: dotnet restore src/App/App.csproj --locked-mode\n"
                "  verify:\n"
                "    steps:\n"
                "      - run: python tools/dotnet_package_rights.py "
                "--verify-restored "
                "--packages-root \"${{ env.NUGET_PACKAGES }}\" "
                "--project src/App/App.csproj\n",
                encoding="utf-8",
            )
            self.assertIn(
                "DOTNET_PACKAGE_RIGHTS_VERIFY_ORDER_INVALID:src/App/App.csproj",
                package_rights_blockers(root),
            )

    def test_restore_and_verifier_outside_jobs_section_do_not_count(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_project(root)
            _write_policy(root)
            workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "env:\n"
                "  NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages\n"
                "  fake:\n"
                "    - run: dotnet restore src/App/App.csproj --locked-mode\n"
                "    - run: python tools/dotnet_package_rights.py "
                "--verify-restored "
                '--packages-root "${{ env.NUGET_PACKAGES }}" '
                "--project src/App/App.csproj\n"
                "jobs:\n"
                "  actual:\n"
                "    steps:\n"
                "      - run: echo no-op\n",
                encoding="utf-8",
            )
            self.assertIn(
                "DOTNET_PACKAGE_RIGHTS_VERIFY_ORDER_INVALID:src/App/App.csproj",
                package_rights_blockers(root),
            )


    def test_verifier_text_inside_run_block_does_not_count(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "env:\n"
                "  NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages\n"
                "jobs:\n"
                "  verify:\n"
                "    steps:\n"
                "      - run: dotnet restore src/App/App.csproj --locked-mode\n"
                "      - run: |\n"
                "          cat <<'EOF'\n"
                "          - run: python tools/dotnet_package_rights.py "
                "--verify-restored "
                "--packages-root \"${{ env.NUGET_PACKAGES }}\" "
                "--project src/App/App.csproj\n"
                "          EOF\n",
                encoding="utf-8",
            )
            blockers = package_rights_blockers(root)
            self.assertIn(
                "DOTNET_PACKAGE_RIGHTS_VERIFY_PROJECT_MISSING:"
                "src/App/App.csproj",
                blockers,
            )
            self.assertTrue(
                any(
                    value.startswith(
                        "DOTNET_PACKAGE_RIGHTS_VERIFY_COMMAND_INVALID:"
                    )
                    for value in blockers
                )
            )

    def test_restore_text_inside_run_block_does_not_count(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "env:\n"
                "  NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages\n"
                "jobs:\n"
                "  verify:\n"
                "    steps:\n"
                "      - run: |\n"
                "          cat <<'EOF'\n"
                "          - run: dotnet restore src/App/App.csproj --locked-mode\n"
                "          EOF\n"
                "      - run: python tools/dotnet_package_rights.py "
                "--verify-restored "
                "--packages-root \"${{ env.NUGET_PACKAGES }}\" "
                "--project src/App/App.csproj\n",
                encoding="utf-8",
            )
            self.assertIn(
                "DOTNET_PACKAGE_RIGHTS_VERIFY_ORDER_INVALID:src/App/App.csproj",
                package_rights_blockers(root),
            )

    def test_second_restore_after_rights_verification_is_blocked(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "env:\n"
                "  NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages\n"
                "jobs:\n"
                "  verify:\n"
                "    steps:\n"
                "      - run: dotnet restore src/App/App.csproj --locked-mode\n"
                "      - run: python tools/dotnet_package_rights.py "
                "--verify-restored "
                '--packages-root "${{ env.NUGET_PACKAGES }}" '
                "--project src/App/App.csproj\n"
                "      - run: dotnet restore src/App/App.csproj --locked-mode\n",
                encoding="utf-8",
            )
            self.assertIn(
                "DOTNET_PACKAGE_RIGHTS_VERIFY_ORDER_INVALID:src/App/App.csproj",
                package_rights_blockers(root),
            )

    def test_post_restore_rights_verifier_rejects_shell_bypass(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "env:\n"
                "  NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages\n"
                "jobs:\n"
                "  verify:\n"
                "    steps:\n"
                "      - run: python tools/dotnet_package_rights.py "
                "--verify-restored "
                '--packages-root "${{ env.NUGET_PACKAGES }}" '
                "--project src/App/App.csproj || true\n",
                encoding="utf-8",
            )
            blockers = package_rights_blockers(root)
            self.assertTrue(
                any(
                    blocker.startswith(
                        "DOTNET_PACKAGE_RIGHTS_VERIFY_COMMAND_INVALID:"
                    )
                    for blocker in blockers
                )
            )
            self.assertIn(
                "DOTNET_PACKAGE_RIGHTS_VERIFY_PROJECT_MISSING:"
                "src/App/App.csproj",
                blockers,
            )

    def test_post_restore_rights_verifier_rejects_step_bypass_controls(self):
        for bypass in (
            "        continue-on-error: true\n",
            "        if: ${{ false }}\n",
            "        shell: echo {0}\n",
        ):
            with self.subTest(bypass=bypass.strip()):
                with TemporaryDirectory() as directory:
                    root = Path(directory)
                    _write_project(root)
                    _write_policy(root)
                    workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
                    workflow.parent.mkdir(parents=True)
                    workflow.write_text(
                        "env:\n"
                        "  NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages\n"
                        "jobs:\n"
                        "  verify:\n"
                        "    steps:\n"
                        "      - name: rights\n"
                        "        run: python tools/dotnet_package_rights.py "
                        "--verify-restored "
                        '--packages-root "${{ env.NUGET_PACKAGES }}" '
                        "--project src/App/App.csproj\n"
                        + bypass,
                        encoding="utf-8",
                    )
                    blockers = package_rights_blockers(root)
                    self.assertTrue(
                        any(
                            blocker.startswith(
                                "DOTNET_PACKAGE_RIGHTS_VERIFY_COMMAND_INVALID:"
                            )
                            for blocker in blockers
                        )
                    )
                    self.assertIn(
                        "DOTNET_PACKAGE_RIGHTS_VERIFY_PROJECT_MISSING:"
                        "src/App/App.csproj",
                        blockers,
                    )

    def test_zero_package_project_does_not_require_nuget_cache(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "src" / "NoPackages" / "NoPackages.csproj"
            project.parent.mkdir(parents=True)
            project.write_text(
                "<Project Sdk=\"Microsoft.NET.Sdk\">"
                "<PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup>"
                "</Project>\n",
                encoding="utf-8",
            )
            verify_restored_package_rights(
                root / "missing-nuget-cache",
                root=root,
                projects=[project],
            )

    def test_foundation_workflow_verifies_restored_package_rights(self):
        workflow = (
            ROOT / ".github" / "workflows" / "dotnet-foundation.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "NUGET_PACKAGES: ${{ github.workspace }}/.nuget/packages",
            workflow,
        )
        normalized = " ".join(line.strip() for line in workflow.splitlines())
        for project in (
            "src/AutoTrade.Contracts/AutoTrade.Contracts.csproj",
            "src/AutoTrade.Desktop/AutoTrade.Desktop.csproj",
        ):
            with self.subTest(project=project):
                self.assertIn(
                    "--verify-restored "
                    '--packages-root "${{ env.NUGET_PACKAGES }}" '
                    f"--project {project}",
                    normalized,
                )

    def test_exact_locked_package_and_reviewed_license_pass(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            _write_rights_workflow(root, [project])
            packages = _write_restored_package(root)
            self.assertEqual(package_rights_blockers(root), [])
            verify_restored_package_rights(
                packages,
                root=root,
                projects=[project],
            )

    def test_restored_nupkg_payload_must_match_lock_hash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            packages = _write_restored_package(root)
            (
                packages
                / "example.package"
                / "1.2.3"
                / "example.package.1.2.3.nupkg"
            ).write_bytes(b"tampered package payload\n")
            with self.assertRaisesRegex(ValueError, "payload hash mismatch"):
                verify_restored_package_rights(
                    packages,
                    root=root,
                    projects=[project],
                )

    def test_restored_nuspec_identity_must_match_locked_artifact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            packages = _write_restored_package(root)
            nuspec = (
                packages
                / "example.package"
                / "1.2.3"
                / "example.package.nuspec"
            )
            nuspec.write_text(
                "<?xml version=\"1.0\"?>\n"
                "<package><metadata><id>Other.Package</id><version>9.9.9</version>"
                "<license type=\"file\">LICENSE.txt</license></metadata></package>\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "nuspec differs from locked nupkg payload"
            ):
                verify_restored_package_rights(
                    packages,
                    root=root,
                    projects=[project],
                )

    def test_restored_license_drift_fails_even_with_same_policy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            packages = _write_restored_package(
                root,
                license_text=_LICENSE + "extra restriction\n",
            )
            with self.assertRaisesRegex(
                ValueError, "license differs from locked nupkg payload"
            ):
                verify_restored_package_rights(
                    packages,
                    root=root,
                    projects=[project],
                )

    def test_restored_notice_drift_fails_even_with_same_locked_package(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            packages = _write_restored_package(root)
            notice = (
                packages
                / "example.package"
                / "1.2.3"
                / "NOTICE.txt"
            )
            notice.write_text("tampered notice\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "notice differs from locked nupkg payload"
            ):
                verify_restored_package_rights(
                    packages,
                    root=root,
                    projects=[project],
                )

    def test_restored_nuspec_semantic_noop_drift_still_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            packages = _write_restored_package(root)
            nuspec = (
                packages
                / "example.package"
                / "1.2.3"
                / "example.package.nuspec"
            )
            nuspec.write_text(
                _NUSPEC.replace("<metadata>", "<metadata>\n"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError, "nuspec differs from locked nupkg payload"
            ):
                verify_restored_package_rights(
                    packages,
                    root=root,
                    projects=[project],
                )

    def test_desktop_client_lock_matches_desktop_webview_dependency(self):
        desktop_lock = json.loads(
            (ROOT / "src" / "AutoTrade.Desktop" / "packages.lock.json").read_text(
                encoding="utf-8"
            )
        )
        client_lock = json.loads(
            (ROOT / "tests" / "Desktop.Client" / "packages.lock.json").read_text(
                encoding="utf-8"
            )
        )
        target = "net10.0-windows7.0"
        desktop = desktop_lock["dependencies"][target]["Microsoft.Web.WebView2"]
        client = client_lock["dependencies"][target]["Microsoft.Web.WebView2"]
        self.assertEqual(client["resolved"], desktop["resolved"])
        self.assertEqual(client["contentHash"], desktop["contentHash"])
        self.assertEqual(
            client_lock["dependencies"][target]["autotrade.desktop"]["dependencies"][
                "Microsoft.Web.WebView2"
            ],
            f"[{desktop['resolved']}, )",
        )


if __name__ == "__main__":
    unittest.main()
