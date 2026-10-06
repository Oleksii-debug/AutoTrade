from __future__ import annotations

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.dotnet_package_rights import (
    ROOT,
    package_rights_blockers,
    verify_restored_package_rights,
)


_HASH = base64.b64encode(b"x" * 64).decode("ascii")
_LICENSE = """Copyright (C) Example Corporation. All rights reserved.

Redistribution in binary form is permitted when this notice is retained.
"""


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
    (package / "LICENSE.txt").write_text(license_text, encoding="utf-8")
    (package / "NOTICE.txt").write_text("required notice\n", encoding="utf-8")
    (package / "example.package.nuspec").write_text(
        "<?xml version=\"1.0\"?>\n"
        "<package><metadata><id>Example.Package</id><version>1.2.3</version>"
        "<license type=\"file\">LICENSE.txt</license></metadata></package>\n",
        encoding="utf-8",
    )
    return packages


class DotnetPackageRightsTests(unittest.TestCase):
    def test_repository_locked_graph_has_exact_rights_coverage(self):
        self.assertEqual(package_rights_blockers(ROOT), [])

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

    def test_restored_license_drift_fails_even_with_same_policy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = _write_project(root)
            _write_policy(root)
            packages = _write_restored_package(
                root,
                license_text=_LICENSE + "extra restriction\n",
            )
            with self.assertRaisesRegex(ValueError, "license differs from reviewed text"):
                verify_restored_package_rights(
                    packages,
                    root=root,
                    projects=[project],
                )


if __name__ == "__main__":
    unittest.main()
