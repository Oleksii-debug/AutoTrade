import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.check_dependency_composition import (
    _ci_runtime_blockers,
    _dotnet_blockers,
    _dotnet_dependency_lock_blockers,
    _python_blockers,
    _rights_blockers,
    audit_composition,
    is_exact_python_requirement,
    qualification_exit_code,
)


class DependencyCompositionGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = audit_composition()

    def test_current_tree_is_not_falsely_declared_release_qualified(self):
        self.assertFalse(self.report.qualified)
        self.assertTrue(self.report.blockers)

    def test_report_mode_stays_usable_while_strict_release_mode_fails_closed(self):
        self.assertEqual(
            qualification_exit_code(self.report, require_qualified=False),
            0,
        )
        self.assertEqual(
            qualification_exit_code(self.report, require_qualified=True),
            1,
        )

    def test_runtime_python_requirements_are_exact(self):
        self.assertGreater(len(self.report.exact_python_requirements), 0)
        self.assertTrue(
            all("==" in item for item in self.report.exact_python_requirements)
        )
        self.assertFalse(
            any(
                blocker.startswith("NON_EXACT_PYTHON_REQUIREMENT:")
                for blocker in self.report.blockers
            )
        )

    def test_python_development_graph_is_hash_locked(self):
        self.assertFalse(
            any(
                blocker.startswith("MISSING_PYTHON_REQUIREMENT_HASH:")
                or blocker.startswith("DUPLICATE_PYTHON_REQUIREMENT_HASH:")
                or blocker == "UNREADABLE_PYTHON_HASH_LOCK"
                for blocker in self.report.blockers
            )
        )

    def test_exact_python_pin_rejects_wildcards_markers_and_ranges(self):
        self.assertTrue(is_exact_python_requirement("attrs==26.1.0"))
        for value in (
            "attrs==26.*",
            "attrs==26.1.0;python_version>='3.12'",
            "attrs==26.1.0,!=26.1.1",
            "attrs>=26.1.0",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_exact_python_requirement(value))

    def test_research_build_dependency_is_exact(self):
        self.assertNotIn("MISSING_RESEARCH_BUILD_REQUIREMENTS", self.report.blockers)
        self.assertFalse(
            any(
                blocker.startswith("NON_EXACT_RESEARCH_BUILD_REQUIREMENT:")
                for blocker in self.report.blockers
            )
        )
        pyproject = (
            Path(__file__).resolve().parents[2] / "research" / "pyproject.toml"
        ).read_text(encoding="utf-8")
        self.assertIn('requires = ["setuptools==84.0.0"]', pyproject)
        self.assertNotIn("RESEARCH_BUILD_REQUIREMENTS_DRIFT", self.report.blockers)
        requirements = (
            Path(__file__).resolve().parents[2] / "requirements-dev.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("setuptools==84.0.0", requirements)
        self.assertIn(
            "--hash=sha256:51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670",
            requirements,
        )

    def test_root_build_dependency_is_exact_and_hash_qualified(self):
        for blocker in (
            "MALFORMED_ROOT_BUILD_SYSTEM",
            "MISSING_ROOT_BUILD_REQUIREMENTS",
            "MALFORMED_ROOT_BUILD_REQUIREMENT",
            "ROOT_BUILD_REQUIREMENTS_DRIFT",
        ):
            self.assertNotIn(blocker, self.report.blockers)
        self.assertFalse(
            any(
                blocker.startswith("NON_EXACT_ROOT_BUILD_REQUIREMENT:")
                for blocker in self.report.blockers
            )
        )
        root = Path(__file__).resolve().parents[2]
        root_pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('requires = ["setuptools==84.0.0"]', root_pyproject)
        requirements = (root / "requirements-dev.txt").read_text(encoding="utf-8")
        self.assertIn("setuptools==84.0.0", requirements)

    def test_root_build_dependency_drift_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "research").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / "requirements-dev.txt").write_text(
                "setuptools==84.0.0 \\\n"
                "    --hash=sha256:"
                "51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670\n"
                "jsonschema==4.26.0 \\\n"
                "    --hash=sha256:"
                "d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce\n",
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                "[build-system]\n"
                'requires = ["wheel>=1"]\n\n'
                "[project]\n"
                'name = "autotrade-exact-numeric"\n'
                'version = "0.0.1"\n',
                encoding="utf-8",
            )
            (root / "research" / "pyproject.toml").write_text(
                "[build-system]\n"
                'requires = ["setuptools==84.0.0"]\n\n'
                "[project]\n"
                'name = "sample"\n'
                'version = "0.0.1"\n'
                'dependencies = ["autotrade-exact-numeric==0.0.1"]\n\n'
                "[project.optional-dependencies]\n"
                'test = ["jsonschema==4.26.0"]\n',
                encoding="utf-8",
            )
            (root / ".github" / "workflows" / "research-primitives.yml").write_text(
                'paths:\n  - "requirements-dev.txt"\n'
                'steps:\n'
                '  - run: "python -m pip install --disable-pip-version-check '
                '--force-reinstall --no-deps --only-binary=:all: --require-hashes '
                '-r requirements-dev.txt"\n'
                '  - run: python -m pip install --no-deps --no-build-isolation -e research\n',
                encoding="utf-8",
            )
            blockers, _ = _python_blockers(root)
            self.assertIn("NON_EXACT_ROOT_BUILD_REQUIREMENT:wheel>=1", blockers)
            self.assertIn("ROOT_BUILD_REQUIREMENTS_DRIFT", blockers)

    def test_research_runtime_dependency_is_exact_and_source_bound(self):
        for blocker in (
            "MISSING_RESEARCH_RUNTIME_REQUIREMENTS",
            "RESEARCH_RUNTIME_REQUIREMENTS_DRIFT",
            "UNREADABLE_ROOT_PYPROJECT",
            "MALFORMED_ROOT_PROJECT",
            "MALFORMED_ROOT_PROJECT_IDENTITY",
        ):
            self.assertNotIn(blocker, self.report.blockers)
        self.assertFalse(
            any(
                blocker.startswith("NON_EXACT_RESEARCH_RUNTIME_REQUIREMENT:")
                for blocker in self.report.blockers
            )
        )
        root = Path(__file__).resolve().parents[2]
        research = (root / "research" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'dependencies = ["autotrade-exact-numeric==0.0.1"]',
            research,
        )

    def test_research_runtime_dependency_drift_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "research").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / "requirements-dev.txt").write_text(
                "setuptools==84.0.0 \\\n"
                "    --hash=sha256:"
                "51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670\n"
                "jsonschema==4.26.0 \\\n"
                "    --hash=sha256:"
                "d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce\n",
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                "[project]\n"
                'name = "autotrade-exact-numeric"\n'
                'version = "0.0.1"\n',
                encoding="utf-8",
            )
            (root / "research" / "pyproject.toml").write_text(
                "[build-system]\n"
                'requires = ["setuptools==84.0.0"]\n\n'
                "[project]\n"
                'name = "sample"\n'
                'version = "0.0.1"\n'
                'dependencies = ["unreviewed-runtime==9.9.9"]\n\n'
                "[project.optional-dependencies]\n"
                'test = ["jsonschema==4.26.0"]\n',
                encoding="utf-8",
            )
            (root / ".github" / "workflows" / "research-primitives.yml").write_text(
                'paths:\n  - "requirements-dev.txt"\n'
                'steps:\n'
                '  - run: "python -m pip install --disable-pip-version-check '
                '--force-reinstall --no-deps --only-binary=:all: --require-hashes '
                '-r requirements-dev.txt"\n'
                '  - run: python -m pip install --no-deps --no-build-isolation -e research\n',
                encoding="utf-8",
            )
            blockers, _ = _python_blockers(root)
            self.assertIn("RESEARCH_RUNTIME_REQUIREMENTS_DRIFT", blockers)
            self.assertNotIn(
                "NON_EXACT_RESEARCH_RUNTIME_REQUIREMENT:unreviewed-runtime==9.9.9",
                blockers,
            )

    def test_research_install_workflow_closes_build_isolation_escape(self):
        root = Path(__file__).resolve().parents[2]
        workflow = (
            root / ".github" / "workflows" / "research-primitives.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('- "requirements-dev.txt"', workflow)
        self.assertIn(
            'run: "python -m pip install --disable-pip-version-check '
            "--force-reinstall --no-deps --only-binary=:all: --require-hashes "
            '-r requirements-dev.txt"',
            workflow,
        )
        self.assertIn(
            "run: python -m pip install --no-deps --no-build-isolation -e research",
            workflow,
        )
        self.assertNotIn(
            "run: python -m pip install --no-deps -e research",
            workflow,
        )
        for blocker in (
            "RESEARCH_HASH_LOCK_WORKFLOW_PATH_MISSING",
            "RESEARCH_HASHED_INSTALL_COMMAND_MISSING",
            "RESEARCH_EDITABLE_NO_BUILD_ISOLATION_MISSING",
        ):
            self.assertNotIn(blocker, self.report.blockers)

    def test_all_ci_requirements_installs_use_hash_only_boundary(self):
        self.assertFalse(
            any(
                blocker.startswith("PYTHON_REQUIREMENTS_INSTALL_NOT_HASH_ONLY:")
                for blocker in self.report.blockers
            )
        )

    def test_weak_ci_requirements_install_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "research").mkdir()
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (root / "requirements-dev.txt").write_text(
                "setuptools==84.0.0 \\\n"
                "    --hash=sha256:"
                "51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670\n"
                "jsonschema==4.26.0 \\\n"
                "    --hash=sha256:"
                "d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce\n",
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                "[build-system]\n"
                'requires = ["setuptools==84.0.0"]\n\n'
                "[project]\n"
                'name = "autotrade-exact-numeric"\n'
                'version = "0.0.1"\n',
                encoding="utf-8",
            )
            (root / "research" / "pyproject.toml").write_text(
                "[build-system]\n"
                'requires = ["setuptools==84.0.0"]\n\n'
                "[project]\n"
                'name = "sample"\n'
                'version = "0.0.1"\n'
                'dependencies = ["autotrade-exact-numeric==0.0.1"]\n\n'
                "[project.optional-dependencies]\n"
                'test = ["jsonschema==4.26.0"]\n',
                encoding="utf-8",
            )
            strict = (
                'run: "python -m pip install --disable-pip-version-check '
                '--force-reinstall --no-deps --only-binary=:all: --require-hashes '
                '-r requirements-dev.txt"'
            )
            (workflows / "research-primitives.yml").write_text(
                'paths:\n  - "requirements-dev.txt"\nsteps:\n'
                f"  - {strict}\n"
                "  - run: python -m pip install --no-deps --no-build-isolation -e research\n",
                encoding="utf-8",
            )
            (workflows / "unsafe.yml").write_text(
                "steps:\n"
                "  - run: python -m pip install -r requirements-dev.txt\n",
                encoding="utf-8",
            )
            blockers, _ = _python_blockers(root)
            self.assertIn(
                "PYTHON_REQUIREMENTS_INSTALL_NOT_HASH_ONLY:"
                ".github/workflows/unsafe.yml:2",
                blockers,
            )

    def test_research_build_boundary_regression_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "research").mkdir()
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / "requirements-dev.txt").write_text(
                """setuptools==84.0.0 \\
    --hash=sha256:51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670
jsonschema==4.26.0 \\
    --hash=sha256:d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce
""",
                encoding="utf-8",
            )
            pyproject = root / "research" / "pyproject.toml"
            pyproject.write_text(
                """[build-system]
requires = ["setuptools==84.0.0"]

[project]
name = "sample"
version = "0.0.1"

[project.optional-dependencies]
test = ["jsonschema==4.26.0"]
""",
                encoding="utf-8",
            )
            workflow = root / ".github" / "workflows" / "research-primitives.yml"
            workflow.write_text(
                """paths:
  - "requirements-dev.txt"
steps:
  - run: "python -m pip install --disable-pip-version-check --force-reinstall --no-deps --only-binary=:all: --require-hashes -r requirements-dev.txt"
  - run: python -m pip install --no-deps -e research
""",
                encoding="utf-8",
            )
            blockers, _ = _python_blockers(root)
            self.assertIn(
                "RESEARCH_EDITABLE_NO_BUILD_ISOLATION_MISSING",
                blockers,
            )
            self.assertNotIn("UNREADABLE_PYTHON_HASH_LOCK", blockers)
            self.assertNotIn("UNREADABLE_RESEARCH_PYPROJECT", blockers)
            self.assertNotIn("RESEARCH_HASHED_INSTALL_COMMAND_MISSING", blockers)
            self.assertNotIn("RESEARCH_BUILD_REQUIREMENTS_DRIFT", blockers)
            self.assertNotIn("RESEARCH_TEST_REQUIREMENTS_DRIFT", blockers)

            pyproject.write_text(
                """[build-system]
requires = [["setuptools==84.0.0"]]

[project]
name = "sample"
version = "0.0.1"

[project.optional-dependencies]
test = [["jsonschema==4.26.0"]]
""",
                encoding="utf-8",
            )
            malformed, _ = _python_blockers(root)
            self.assertIn("MALFORMED_RESEARCH_BUILD_REQUIREMENT", malformed)
            self.assertIn("MALFORMED_RESEARCH_TEST_REQUIREMENT", malformed)

            pyproject.write_text(
                """build-system = "not-a-table"
project = "not-a-table"
""",
                encoding="utf-8",
            )
            malformed_tables, _ = _python_blockers(root)
            self.assertIn("MALFORMED_RESEARCH_BUILD_SYSTEM", malformed_tables)
            self.assertIn("MALFORMED_RESEARCH_PROJECT", malformed_tables)
            self.assertIn(
                "MALFORMED_RESEARCH_OPTIONAL_DEPENDENCIES",
                malformed_tables,
            )
            self.assertIn(
                "MISSING_RESEARCH_BUILD_REQUIREMENTS",
                malformed_tables,
            )
            self.assertIn(
                "MISSING_RESEARCH_TEST_REQUIREMENTS",
                malformed_tables,
            )

    def test_research_test_extra_matches_exact_resolved_graph(self):
        self.assertFalse(
            any(
                blocker.startswith("NON_EXACT_RESEARCH_TEST_REQUIREMENT:")
                or blocker == "MISSING_RESEARCH_TEST_REQUIREMENTS"
                or blocker == "RESEARCH_TEST_REQUIREMENTS_DRIFT"
                for blocker in self.report.blockers
            )
        )

    def test_research_test_extra_range_and_graph_drift_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "research").mkdir()
            (root / "requirements-dev.txt").write_text(
                "jsonschema==4.26.0\nreferencing==0.36.2\n",
                encoding="utf-8",
            )
            (root / "research" / "pyproject.toml").write_text(
                """[build-system]\nrequires = ["setuptools==84.0.0"]\n\n[project]\nname = "sample"\nversion = "0.0.1"\n\n[project.optional-dependencies]\ntest = ["jsonschema>=4.23,<5"]\n""",
                encoding="utf-8",
            )
            blockers, exact = _python_blockers(root)
            self.assertEqual(
                exact,
                ["jsonschema==4.26.0", "referencing==0.36.2"],
            )
            self.assertIn(
                "NON_EXACT_RESEARCH_TEST_REQUIREMENT:jsonschema>=4.23,<5",
                blockers,
            )
            self.assertIn("RESEARCH_TEST_REQUIREMENTS_DRIFT", blockers)
            self.assertIn(
                "MISSING_PYTHON_REQUIREMENT_HASH:jsonschema==4.26.0",
                blockers,
            )
            self.assertIn(
                "MISSING_PYTHON_REQUIREMENT_HASH:referencing==0.36.2",
                blockers,
            )

    def test_current_tree_has_no_nuget_lock_protocol_gap(self):
        lock_blockers = {
            blocker
            for blocker in self.report.blockers
            if blocker.startswith("DOTNET_PROJECT_LOCK_MISSING:")
            or blocker.startswith("DOTNET_RESTORE_NOT_LOCKED:")
            or blocker.startswith("DOTNET_LOCKED_RESTORE_PROJECT_MISSING:")
            or blocker in {
                "DOTNET_LOCKED_RESTORE_WORKFLOW_MISSING",
                "DOTNET_LOCKED_RESTORE_COMMAND_MISSING",
                "DOTNET_LOCK_WORKFLOW_PATH_MISSING",
            }
        }
        self.assertEqual(lock_blockers, set())

    def test_nuget_lock_is_per_project_and_restore_is_locked(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "src" / "ReleaseApp" / "ReleaseApp.csproj"
            project.parent.mkdir(parents=True)
            project.write_text("<Project />\n", encoding="utf-8")
            unrelated = root / "src" / "Other" / "packages.lock.json"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("{}\n", encoding="utf-8")

            workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                'paths:\n'
                '  - "src/**/packages.lock.json"\n'
                "steps:\n"
                "  - run: dotnet restore src/ReleaseApp/ReleaseApp.csproj --locked-mode\n",
                encoding="utf-8",
            )

            blockers = _dotnet_dependency_lock_blockers(root, [project])
            self.assertEqual(
                blockers,
                ["DOTNET_PROJECT_LOCK_MISSING:src/ReleaseApp/ReleaseApp.csproj"],
            )

            (project.parent / "packages.lock.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "dependencies": {
                            "net10.0": {
                                "ReleaseApp": {"type": "Project"}
                            }
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project]),
                [],
            )

            unrestored = root / "src" / "Unrestored" / "Unrestored.csproj"
            unrestored.parent.mkdir(parents=True)
            unrestored.write_text("<Project />\n", encoding="utf-8")
            (unrestored.parent / "packages.lock.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "dependencies": {
                            "net10.0": {
                                "ReleaseApp": {"type": "Project"}
                            }
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project, unrestored]),
                [
                    "DOTNET_LOCKED_RESTORE_PROJECT_MISSING:"
                    "src/Unrestored/Unrestored.csproj"
                ],
            )

            workflow.write_text(
                'paths:\n'
                '  - "src/**/packages.lock.json"\n'
                "steps:\n"
                "  - run: dotnet restore src/ReleaseApp/ReleaseApp.csproj\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project]),
                [
                    "DOTNET_RESTORE_NOT_LOCKED:"
                    ".github/workflows/dotnet-foundation.yml:1"
                ],
            )

            workflow.write_text(
                'paths:\n'
                '  - "src/**/packages.lock.json"\n'
                "steps:\n"
                "  - run: dotnet restore src/ReleaseApp/ReleaseApp.csproj "
                "-p:Fake=RestoreLockedMode=true\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project]),
                [
                    "DOTNET_RESTORE_NOT_LOCKED:"
                    ".github/workflows/dotnet-foundation.yml:1"
                ],
            )

            workflow.write_text(
                'paths:\n'
                '  - "src/**/packages.lock.json"\n'
                "steps:\n"
                "  - run: dotnet restore src/ReleaseApp/ReleaseApp.csproj "
                "-p:RestoreLockedMode=true\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project]),
                [],
            )

            workflow.write_text(
                'paths:\n'
                '  - "src/**/packages.lock.json"\n'
                "steps:\n"
                "  - run: |\n"
                "      dotnet restore src/ReleaseApp/ReleaseApp.csproj --locked-mode\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project]),
                [
                    "DOTNET_RESTORE_COMMAND_UNSCOPED:"
                    ".github/workflows/dotnet-foundation.yml:5",
                    "DOTNET_LOCKED_RESTORE_COMMAND_MISSING",
                ],
            )


            workflow.write_text(
                'paths:\n'
                '  - "src/**/packages.lock.json"\n'
                "steps:\n"
                "  - run: dotnet restore src/ReleaseApp/ReleaseApp.csproj --locked-mode\n"
                "  - run: >\n"
                "      dotnet\n"
                "      restore src/ReleaseApp/ReleaseApp.csproj\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project]),
                [
                    "DOTNET_RESTORE_COMMAND_UNSCOPED:"
                    ".github/workflows/dotnet-foundation.yml:6"
                ],
            )


            workflow.write_text(
                'paths:\n'
                '  - "src/**/packages.lock.json"\n'
                "steps:\n"
                "  - run: dotnet restore src/ReleaseApp/ReleaseApp.csproj "
                "--locked-mode && dotnet restore src/ReleaseApp/ReleaseApp.csproj\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project]),
                [
                    "DOTNET_RESTORE_COMMAND_INVALID:"
                    ".github/workflows/dotnet-foundation.yml:1",
                    "DOTNET_LOCKED_RESTORE_PROJECT_MISSING:"
                    "src/ReleaseApp/ReleaseApp.csproj",
                ],
            )


            workflow.write_text(
                'paths:\n'
                '  - "src/**/packages.lock.json"\n'
                "steps:\n"
                "  - run: dotnet restore src/ReleaseApp/ReleaseApp.csproj "
                "--locked-mode --force-evaluate\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project]),
                [
                    "DOTNET_RESTORE_NOT_LOCKED:"
                    ".github/workflows/dotnet-foundation.yml:1"
                ],
            )


            workflow.write_text(
                'paths:\n'
                '  - "src/**/packages.lock.json"\n'
                "steps:\n"
                "  - run: dotnet restore src/ReleaseApp/ReleaseApp.csproj "
                "--locked-mode --lock-file-path artifacts/other.lock.json\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project]),
                [
                    "DOTNET_RESTORE_NOT_LOCKED:"
                    ".github/workflows/dotnet-foundation.yml:1"
                ],
            )


            workflow.write_text(
                'paths:\n'
                '  - "src/**/packages.lock.json"\n'
                "steps:\n"
                "  - run: dotnet restore src/Missing/Missing.csproj --locked-mode\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project]),
                [
                    "DOTNET_RESTORE_PROJECT_NOT_FOUND:"
                    ".github/workflows/dotnet-foundation.yml:1:"
                    "src/Missing/Missing.csproj",
                    "DOTNET_LOCKED_RESTORE_PROJECT_MISSING:"
                    "src/ReleaseApp/ReleaseApp.csproj",
                ],
            )


    def test_dotnet_restore_authority_helpers_are_imported_in_both_execution_modes(self):
        root = Path(__file__).resolve().parents[2]
        for relative in (
            "tools/check_dependency_composition.py",
            "tools/build_provenance_manifest.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            for helper in (
                "dotnet_restore_project_target,",
                "dotnet_restore_workflow_environment_authority_lines,",
            ):
                with self.subTest(relative=relative, helper=helper):
                    self.assertEqual(source.count(helper), 2, relative)

    def test_unified_wp03_audit_executes_nuget_rights_gate(self):
        self.assertFalse(
            any(
                blocker.startswith("DOTNET_PACKAGE_RIGHTS_")
                for blocker in self.report.blockers
            )
        )
        source = (
            Path(__file__).resolve().parents[2]
            / "tools"
            / "check_dependency_composition.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(source.count("package_rights_blockers"), 3)


            workflow.write_text(
                'paths:\n'
                '  - "src/**/packages.lock.json"\n'
                "env:\n"
                "  RestoreForceEvaluate: true\n"
                "steps:\n"
                "  - run: dotnet restore src/ReleaseApp/ReleaseApp.csproj --locked-mode\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project]),
                [
                    "DOTNET_RESTORE_ENVIRONMENT_AUTHORITY_UNSUPPORTED:"
                    ".github/workflows/dotnet-foundation.yml:"
                    "4:RestoreForceEvaluate"
                ],
            )

    def test_nuget_lock_changes_must_trigger_dotnet_workflow(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "src" / "ReleaseApp" / "ReleaseApp.csproj"
            project.parent.mkdir(parents=True)
            project.write_text("<Project />\n", encoding="utf-8")
            (project.parent / "packages.lock.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "dependencies": {
                            "net10.0": {
                                "ReleaseApp": {"type": "Project"}
                            }
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            workflow = root / ".github" / "workflows" / "dotnet-foundation.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "steps:\n"
                "  - run: dotnet restore src/ReleaseApp/ReleaseApp.csproj --locked-mode\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _dotnet_dependency_lock_blockers(root, [project]),
                ["DOTNET_LOCK_WORKFLOW_PATH_MISSING"],
            )

    def test_dotnet_sdk_is_exact_and_roll_forward_is_disabled(self):
        self.assertEqual(self.report.dotnet_sdk, "10.0.100")
        self.assertFalse(
            any(
                blocker.startswith("DOTNET_ROLL_FORWARD_NOT_DISABLED:")
                for blocker in self.report.blockers
            )
        )

    def test_omitted_dotnet_roll_forward_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "global.json").write_text(
                json.dumps({"sdk": {"version": "10.0.100"}}),
                encoding="utf-8",
            )
            blockers, references, version = _dotnet_blockers(root)
            self.assertEqual(version, "10.0.100")
            self.assertEqual(references, [])
            self.assertIn(
                "DOTNET_ROLL_FORWARD_NOT_DISABLED:None",
                blockers,
            )

    def test_dotnet_ci_installs_the_same_exact_sdk(self):
        root = Path(__file__).resolve().parents[2]
        foundation = (root / ".github" / "workflows" / "dotnet-foundation.yml").read_text(
            encoding="utf-8"
        )
        lean = (root / ".github" / "workflows" / "lean-adoption.yml").read_text(
            encoding="utf-8"
        )
        contracts = (root / ".github" / "workflows" / "contracts.yml").read_text(
            encoding="utf-8"
        )
        verify = (root / ".github" / "workflows" / "verify.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(foundation.count('dotnet-version: "10.0.100"'), 2)
        self.assertEqual(lean.count('dotnet-version: "10.0.100"'), 1)
        self.assertEqual(contracts.count('dotnet-version: "10.0.100"'), 1)
        self.assertEqual(verify.count('dotnet-version: "10.0.100"'), 1)
        for workflow in (foundation, lean, contracts, verify):
            self.assertNotIn('dotnet-version: "10.0.x"', workflow)

    def test_ci_python_runtime_is_exact(self):
        blockers = {
            item
            for item in self.report.blockers
            if item.startswith("NON_EXACT_CI_PYTHON_VERSION:")
        }
        self.assertEqual(blockers, set())

    def test_ci_actions_are_immutable(self):
        blockers = {
            item
            for item in self.report.blockers
            if item.startswith("NON_IMMUTABLE_CI_ACTION:")
        }
        self.assertEqual(blockers, set())

    def test_mutable_ci_action_tag_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "mutable.yml").write_text(
                "steps:\n"
                "  - uses: actions/checkout@v4\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _ci_runtime_blockers(root),
                [
                    "NON_IMMUTABLE_CI_ACTION:"
                    ".github/workflows/mutable.yml:2:actions/checkout@v4"
                ],
            )

    def test_expression_valued_python_runtime_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            workflow = workflows / "expression.yml"
            workflow.write_text(
                "steps:\n"
                "  - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065\n"
                "    with:\n"
                "      python-version: ${{ matrix.python-version }}\n",
                encoding="utf-8",
            )
            self.assertEqual(
                _ci_runtime_blockers(root),
                [
                    "UNRESOLVED_CI_PYTHON_VERSION:"
                    ".github/workflows/expression.yml:"
                    "${{ matrix.python-version }}"
                ],
            )

    def test_unresolved_first_party_rights_remain_fail_closed(self):
        unresolved = {
            blocker
            for blocker in self.report.blockers
            if blocker.startswith("UNRESOLVED_COMPONENT_RIGHTS:")
        }
        self.assertEqual(
            unresolved,
            {
                "UNRESOLVED_COMPONENT_RIGHTS:Autosport first-party source",
                "UNRESOLVED_COMPONENT_RIGHTS:Nika Core first-party source",
            },
        )

    def test_pending_external_composition_is_not_release_approved(self):
        pending = {
            blocker
            for blocker in self.report.blockers
            if blocker.startswith("UNQUALIFIED_SOURCE_COMPOSITION:")
        }
        self.assertIn(
            "UNQUALIFIED_SOURCE_COMPOSITION:QuantConnect LEAN",
            pending,
        )
        self.assertIn(
            "UNQUALIFIED_SOURCE_COMPOSITION:WhiteBit.Net",
            pending,
        )
        self.assertIn(
            "UNQUALIFIED_SOURCE_COMPOSITION:CryptoExchange.Net",
            pending,
        )
        self.assertIn(
            "UNQUALIFIED_SOURCE_COMPOSITION:Alpaca official C# SDK",
            pending,
        )


    def test_machine_release_state_blocks_free_text_bypass(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provenance = root / "provenance"
            provenance.mkdir()
            (provenance / "components.json").write_text(
                json.dumps(
                    {
                        "components": [
                            {
                                "name": "Candidate",
                                "repository": "owner/repo",
                                "revision": "a" * 40,
                                "license": "MIT",
                                "source_import_allowed": "QUALIFIED",
                                "release_distribution_state": "BLOCKED",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            blockers = _rights_blockers(root)
            self.assertIn(
                "COMPONENT_RELEASE_DISTRIBUTION_NOT_APPROVED:Candidate",
                blockers,
            )
            self.assertNotIn(
                "UNQUALIFIED_SOURCE_COMPOSITION:Candidate",
                blockers,
            )

    def test_approved_component_requires_resolvable_composition_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provenance = root / "provenance"
            evidence_dir = provenance / "evidence"
            evidence_dir.mkdir(parents=True)
            payloads = {
                "dependency-graph.json": b'{"dependencies":[]}',
                "NOTICE.txt": b"Apache-2.0 notice evidence\n",
                "advisory-review.json": b'{"advisories":[]}',
            }
            for filename, payload in payloads.items():
                (evidence_dir / filename).write_bytes(payload)

            base = {
                "name": "ApprovedCandidate",
                "repository": "owner/repo",
                "revision": "b" * 40,
                "license": "Apache-2.0",
                "source_import_allowed": "QUALIFIED",
                "release_distribution_state": "APPROVED",
            }
            (provenance / "components.json").write_text(
                json.dumps({"components": [base]}),
                encoding="utf-8",
            )

            blockers = _rights_blockers(root)
            self.assertEqual(
                {
                    blocker
                    for blocker in blockers
                    if blocker.startswith("APPROVED_COMPONENT_EVIDENCE_INVALID:")
                },
                {
                    "APPROVED_COMPONENT_EVIDENCE_INVALID:ApprovedCandidate:"
                    "dependency_graph_sha256",
                    "APPROVED_COMPONENT_EVIDENCE_INVALID:ApprovedCandidate:"
                    "notice_sha256",
                    "APPROVED_COMPONENT_EVIDENCE_INVALID:ApprovedCandidate:"
                    "advisory_review_sha256",
                },
            )

            qualified = dict(base)
            qualified.update(
                {
                    "dependency_graph_sha256": "sha256:"
                    + sha256(payloads["dependency-graph.json"]).hexdigest(),
                    "dependency_graph_evidence_path": (
                        "provenance/evidence/dependency-graph.json"
                    ),
                    "notice_sha256": "sha256:"
                    + sha256(payloads["NOTICE.txt"]).hexdigest(),
                    "notice_evidence_path": "provenance/evidence/NOTICE.txt",
                    "advisory_review_sha256": "sha256:"
                    + sha256(payloads["advisory-review.json"]).hexdigest(),
                    "advisory_review_evidence_path": (
                        "provenance/evidence/advisory-review.json"
                    ),
                }
            )
            (provenance / "components.json").write_text(
                json.dumps({"components": [qualified]}),
                encoding="utf-8",
            )
            self.assertEqual(
                _rights_blockers(root),
                [
                    "APPROVED_COMPONENT_INDEPENDENT_AUTHORITY_REQUIRED:"
                    "ApprovedCandidate"
                ],
            )

            forged = dict(qualified)
            forged["notice_sha256"] = "sha256:" + "2" * 64
            (provenance / "components.json").write_text(
                json.dumps({"components": [forged]}),
                encoding="utf-8",
            )
            self.assertIn(
                "APPROVED_COMPONENT_EVIDENCE_DIGEST_MISMATCH:"
                "ApprovedCandidate:notice_sha256",
                _rights_blockers(root),
            )

    def test_approved_component_evidence_path_cannot_escape_provenance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provenance = root / "provenance"
            provenance.mkdir()
            outside = root / "outside.txt"
            outside.write_bytes(b"outside")
            digest = "sha256:" + sha256(outside.read_bytes()).hexdigest()
            candidate = {
                "name": "ApprovedCandidate",
                "repository": "owner/repo",
                "revision": "d" * 40,
                "license": "MIT",
                "source_import_allowed": "QUALIFIED",
                "release_distribution_state": "APPROVED",
                "dependency_graph_sha256": digest,
                "dependency_graph_evidence_path": "../outside.txt",
                "notice_sha256": digest,
                "notice_evidence_path": "../outside.txt",
                "advisory_review_sha256": digest,
                "advisory_review_evidence_path": "../outside.txt",
            }
            (provenance / "components.json").write_text(
                json.dumps({"components": [candidate]}),
                encoding="utf-8",
            )
            blockers = _rights_blockers(root)
            self.assertEqual(
                {
                    blocker
                    for blocker in blockers
                    if "EVIDENCE_PATH_INVALID" in blocker
                },
                {
                    "APPROVED_COMPONENT_EVIDENCE_PATH_INVALID:ApprovedCandidate:"
                    "dependency_graph_evidence_path",
                    "APPROVED_COMPONENT_EVIDENCE_PATH_INVALID:ApprovedCandidate:"
                    "notice_evidence_path",
                    "APPROVED_COMPONENT_EVIDENCE_PATH_INVALID:ApprovedCandidate:"
                    "advisory_review_evidence_path",
                },
            )

    def test_duplicate_machine_release_state_is_rejected_as_ambiguous_json(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provenance = root / "provenance"
            provenance.mkdir()
            (provenance / "components.json").write_text(
                """{
  "components": [
    {
      "name": "Candidate",
      "repository": "owner/repo",
      "revision": "cccccccccccccccccccccccccccccccccccccccc",
      "license": "MIT",
      "source_import_allowed": "QUALIFIED",
      "release_distribution_state": "BLOCKED",
      "release_distribution_state": "APPROVED",
      "dependency_graph_sha256": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
      "notice_sha256": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
      "advisory_review_sha256": "sha256:3333333333333333333333333333333333333333333333333333333333333333"
    }
  ]
}""",
                encoding="utf-8",
            )
            self.assertEqual(
                _rights_blockers(root),
                ["COMPONENT_INVENTORY_INVALID_JSON"],
            )

    def test_non_object_component_inventory_root_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provenance = root / "provenance"
            provenance.mkdir()
            (provenance / "components.json").write_text(
                "[]",
                encoding="utf-8",
            )
            self.assertEqual(
                _rights_blockers(root),
                ["COMPONENT_INVENTORY_INVALID_ROOT"],
            )

    def test_component_inventory_and_source_identity_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provenance = root / "provenance"
            provenance.mkdir()

            (provenance / "components.json").write_text(
                json.dumps({"components": []}),
                encoding="utf-8",
            )
            self.assertEqual(
                _rights_blockers(root),
                ["COMPONENT_INVENTORY_MISSING_OR_EMPTY"],
            )

            invalid = {
                "name": " Component ",
                "repository": "owner-only",
                "revision": "main",
                "license": "MIT",
                "source_import_allowed": "QUALIFIED",
                "release_distribution_state": "APPROVED",
                "dependency_graph_sha256": "sha256:" + "1" * 64,
                "notice_sha256": "sha256:" + "2" * 64,
                "advisory_review_sha256": "sha256:" + "3" * 64,
            }
            (provenance / "components.json").write_text(
                json.dumps({"components": [invalid]}),
                encoding="utf-8",
            )
            blockers = _rights_blockers(root)
            self.assertIn("INVALID_COMPONENT_NAME:0", blockers)
            self.assertIn(
                "INVALID_COMPONENT_SOURCE_IDENTITY: Component ",
                blockers,
            )


if __name__ == "__main__":
    unittest.main()
