from __future__ import annotations

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.dotnet_lock import (
    dotnet_imported_package_reference_blockers,
    dotnet_lock_content_blockers,
    dotnet_locked_dependency_graph,
    dotnet_project_package_references,
    dotnet_restore_command_tokens,
    dotnet_restore_targets_project,
    dotnet_restore_tokens_are_locked,
    dotnet_restore_workflow_commands,
)


GOOD_HASH = base64.b64encode(bytes(range(64))).decode('ascii')


def write_project(root: Path, *, version: str = '1.0.4191.47', duplicate: bool = False) -> Path:
    project = root / 'src' / 'AutoTrade.Desktop' / 'AutoTrade.Desktop.csproj'
    project.parent.mkdir(parents=True)
    extra = '<PackageReference Include="Microsoft.Web.WebView2" Version="1.0.4129.50" />' if duplicate else ''
    project.write_text(
        '<Project><ItemGroup>'
        f'<PackageReference Include="Microsoft.Web.WebView2" Version="{version}" />'
        f'{extra}'
        '</ItemGroup></Project>',
        encoding='utf-8',
    )
    return project


def write_lock(project: Path, *, resolved: str = '1.0.4191.47', content_hash: object = GOOD_HASH,
               record_type: str = 'Direct', package_name: str = 'Microsoft.Web.WebView2',
               version: object = 1, include: bool = True) -> None:
    records = {}
    if include:
        records[package_name] = {
            'type': record_type,
            'requested': '[1.0.4191.47, )',
            'resolved': resolved,
            'contentHash': content_hash,
        }
    payload = {'version': version, 'dependencies': {'net10.0-windows7.0': records}}
    (project.parent / 'packages.lock.json').write_text(json.dumps(payload), encoding='utf-8')


class NugetLockGateCandidateTests(unittest.TestCase):
    def test_restore_locked_mode_requires_exact_switch_or_property(self):
        spoof = dotnet_restore_command_tokens(
            'run: dotnet restore src/App/App.csproj '
            '-p:Fake=RestoreLockedMode=true'
        )
        self.assertFalse(dotnet_restore_tokens_are_locked(spoof))

        self.assertTrue(
            dotnet_restore_tokens_are_locked(
                dotnet_restore_command_tokens(
                    'run: dotnet restore src/App/App.csproj --locked-mode'
                )
            )
        )
        self.assertTrue(
            dotnet_restore_tokens_are_locked(
                dotnet_restore_command_tokens(
                    'run: dotnet restore src/App/App.csproj '
                    '-p:RestoreLockedMode=true'
                )
            )
        )
        self.assertFalse(
            dotnet_restore_tokens_are_locked(
                dotnet_restore_command_tokens(
                    'run: dotnet restore src/App/App.csproj '
                    '-p:RestoreLockedMode=false'
                )
            )
        )

        self.assertFalse(
            dotnet_restore_tokens_are_locked(
                dotnet_restore_command_tokens(
                    'run: dotnet restore src/App/App.csproj --locked-mode '
                    '-p:RestoreLockedMode=false'
                )
            )
        )
        self.assertFalse(
            dotnet_restore_tokens_are_locked(
                dotnet_restore_command_tokens(
                    'run: dotnet restore src/App/App.csproj -- --locked-mode'
                )
            )
        )
        self.assertTrue(
            dotnet_restore_tokens_are_locked(
                dotnet_restore_command_tokens(
                    'run: dotnet restore src/App/App.csproj '
                    '-p:Other=x;RestoreLockedMode=true'
                )
            )
        )

    def test_custom_lock_path_defeats_committed_lock_authority(self):
        self.assertFalse(
            dotnet_restore_tokens_are_locked(
                dotnet_restore_command_tokens(
                    "run: dotnet restore src/App/App.csproj "
                    "--locked-mode --lock-file-path artifacts/other.lock.json"
                )
            )
        )
        self.assertFalse(
            dotnet_restore_tokens_are_locked(
                dotnet_restore_command_tokens(
                    "run: dotnet restore src/App/App.csproj "
                    "-p:RestoreLockedMode=true;"
                    "NuGetLockFilePath=artifacts/other.lock.json"
                )
            )
        )

    def test_force_evaluate_defeats_locked_restore_authority(self):
        self.assertFalse(
            dotnet_restore_tokens_are_locked(
                dotnet_restore_command_tokens(
                    "run: dotnet restore src/App/App.csproj "
                    "--locked-mode --force-evaluate"
                )
            )
        )
        self.assertFalse(
            dotnet_restore_tokens_are_locked(
                dotnet_restore_command_tokens(
                    "run: dotnet restore src/App/App.csproj "
                    "-p:RestoreLockedMode=true;RestoreForceEvaluate=true"
                )
            )
        )
        self.assertTrue(
            dotnet_restore_tokens_are_locked(
                dotnet_restore_command_tokens(
                    "run: dotnet restore src/App/App.csproj "
                    "-p:RestoreLockedMode=true;RestoreForceEvaluate=false"
                )
            )
        )

    def test_yaml_comment_cannot_mint_locked_restore_authority(self):
        tokens = dotnet_restore_command_tokens(
            'run: dotnet restore src/App/App.csproj # --locked-mode'
        )
        self.assertFalse(dotnet_restore_tokens_are_locked(tokens))
        self.assertTrue(
            dotnet_restore_targets_project(tokens, 'src/App/App.csproj')
        )

    def test_block_scalar_restore_is_reported_as_unscoped(self):
        commands, unscoped_lines = dotnet_restore_workflow_commands(
            "steps:\n"
            "  - run: |\n"
            "      dotnet restore src/App/App.csproj --locked-mode\n"
        )
        self.assertEqual(commands, [])
        self.assertEqual(unscoped_lines, [3])

    def test_case_and_spacing_cannot_hide_unscoped_restore(self):
        commands, unscoped_lines = dotnet_restore_workflow_commands(
            "steps:\n"
            "  - run: |\n"
            "      DOTNET   RESTORE src/App/App.csproj --locked-mode\n"
        )
        self.assertEqual(commands, [])
        self.assertEqual(unscoped_lines, [3])

    def test_wrapper_shell_restore_is_unscoped(self):
        commands, unscoped_lines = dotnet_restore_workflow_commands(
            "steps:\n"
            "  - run: pwsh -Command \"dotnet restore src/App/App.csproj --locked-mode\"\n"
        )
        self.assertEqual(commands, [])
        self.assertEqual(unscoped_lines, [2])

    def test_folded_scalar_split_restore_is_reported_as_unscoped(self):
        commands, unscoped_lines = dotnet_restore_workflow_commands(
            "steps:\n"
            "  - run: >\n"
            "      dotnet\n"
            "      restore src/App/App.csproj --locked-mode\n"
            "  - run: dotnet restore src/App/App.csproj --locked-mode\n"
        )
        self.assertEqual(
            commands,
            ["run: dotnet restore src/App/App.csproj --locked-mode"],
        )
        self.assertEqual(unscoped_lines, [3])

    def test_shell_continuation_split_restore_is_reported_as_unscoped(self):
        commands, unscoped_lines = dotnet_restore_workflow_commands(
            "steps:\n"
            "  - run: |\n"
            "      dotnet \\\n"
            "        restore src/App/App.csproj\n"
            "  - run: dotnet restore src/App/App.csproj --locked-mode\n"
        )
        self.assertEqual(
            commands,
            ["run: dotnet restore src/App/App.csproj --locked-mode"],
        )
        self.assertEqual(unscoped_lines, [3])

    def test_restore_subcommand_label_is_not_executable_restore_text(self):
        commands, unscoped_lines = dotnet_restore_workflow_commands(
            "steps:\n"
            "  - run: python tools/check.py "
            "--command \"dotnet restore/build release contract\"\n"
            "  - run: dotnet restore src/App/App.csproj --locked-mode\n"
        )
        self.assertEqual(
            commands,
            ["run: dotnet restore src/App/App.csproj --locked-mode"],
        )
        self.assertEqual(unscoped_lines, [])

    def test_canonical_restore_line_is_discovered_once(self):
        commands, unscoped_lines = dotnet_restore_workflow_commands(
            "steps:\n"
            "  - run: dotnet restore src/App/App.csproj --locked-mode\n"
        )
        self.assertEqual(
            commands,
            ["run: dotnet restore src/App/App.csproj --locked-mode"],
        )
        self.assertEqual(unscoped_lines, [])

    def test_canonical_restore_rejects_shell_chaining_and_substitution(self):
        for command in (
            "run: dotnet restore src/App/App.csproj --locked-mode "
            "&& dotnet restore src/App/App.csproj",
            "run: dotnet restore src/App/App.csproj --locked-mode "
            "$(dotnet restore src/App/App.csproj)",
            "run: dotnet restore src/App/App.csproj --locked-mode "
            "| tee restore.log",
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                    ValueError,
                    "shell execution control",
                ):
                    dotnet_restore_command_tokens(command)

    def test_restore_project_coverage_requires_first_positional_target(self):
        canonical = dotnet_restore_command_tokens(
            'run: dotnet restore src/App/App.csproj --locked-mode'
        )
        self.assertTrue(
            dotnet_restore_targets_project(canonical, 'src/App/App.csproj')
        )

        after_sentinel = dotnet_restore_command_tokens(
            'run: dotnet restore src/Other/Other.csproj --locked-mode '
            '-- src/App/App.csproj'
        )
        self.assertFalse(
            dotnet_restore_targets_project(
                after_sentinel,
                'src/App/App.csproj',
            )
        )

        option_value = dotnet_restore_command_tokens(
            'run: dotnet restore --source src/App/App.csproj --locked-mode'
        )
        self.assertFalse(
            dotnet_restore_targets_project(option_value, 'src/App/App.csproj')
        )

    def test_malformed_restore_command_fails_parsing(self):
        with self.assertRaisesRegex(ValueError, 'malformed dotnet restore command'):
            dotnet_restore_command_tokens(
                'run: dotnet restore "src/App/App.csproj --locked-mode'
            )

    def test_webview2_exact_lock_with_sha512_passes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            write_lock(project)
            self.assertEqual(dotnet_lock_content_blockers(root, project), [])

    def test_missing_lock_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            self.assertEqual(
                dotnet_lock_content_blockers(root, project),
                ['DOTNET_PROJECT_LOCK_MISSING:src/AutoTrade.Desktop/AutoTrade.Desktop.csproj'],
            )

    def test_malformed_lock_json_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            (project.parent / 'packages.lock.json').write_text('{', encoding='utf-8')
            self.assertEqual(
                dotnet_lock_content_blockers(root, project),
                ['DOTNET_PROJECT_LOCK_INVALID_JSON:src/AutoTrade.Desktop/AutoTrade.Desktop.csproj'],
            )

    def test_duplicate_json_keys_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            (project.parent / 'packages.lock.json').write_text(
                '{\"version\":1,\"version\":1,\"dependencies\":{}}',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_lock_content_blockers(root, project),
                ['DOTNET_PROJECT_LOCK_INVALID_JSON:src/AutoTrade.Desktop/AutoTrade.Desktop.csproj'],
            )

    def test_non_finite_lock_json_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            (project.parent / 'packages.lock.json').write_text(
                '{"version":1,"dependencies":{"net10.0-windows7.0":{}},'
                '"unexpected":NaN}',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_lock_content_blockers(root, project),
                ['DOTNET_PROJECT_LOCK_INVALID_JSON:src/AutoTrade.Desktop/AutoTrade.Desktop.csproj'],
            )
            with self.assertRaisesRegex(ValueError, 'does not match project'):
                dotnet_locked_dependency_graph(root, [project])

    def test_unsupported_lock_schema_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            write_lock(project, version=2)
            self.assertEqual(
                dotnet_lock_content_blockers(root, project),
                ['DOTNET_PROJECT_LOCK_VERSION_UNSUPPORTED:src/AutoTrade.Desktop/AutoTrade.Desktop.csproj:2'],
            )

    def test_boolean_lock_schema_version_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            write_lock(project, version=True)
            self.assertEqual(
                dotnet_lock_content_blockers(root, project),
                ['DOTNET_PROJECT_LOCK_VERSION_UNSUPPORTED:src/AutoTrade.Desktop/AutoTrade.Desktop.csproj:True'],
            )
            with self.assertRaisesRegex(ValueError, 'does not match project'):
                dotnet_locked_dependency_graph(root, [project])

    def test_declared_direct_package_must_be_present(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            write_lock(project, include=False)
            self.assertIn(
                'DOTNET_PROJECT_LOCK_DIRECT_MISSING:src/AutoTrade.Desktop/AutoTrade.Desktop.csproj:Microsoft.Web.WebView2@1.0.4191.47',
                dotnet_lock_content_blockers(root, project),
            )

    def test_transitive_record_cannot_satisfy_direct_reference(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            write_lock(project, record_type='Transitive')
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertEqual(len(blockers), 1)
            self.assertIn('DOTNET_PROJECT_LOCK_DIRECT_MISSING:', blockers[0])

    def test_resolved_version_must_equal_project_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            write_lock(project, resolved='1.0.4255-prerelease')
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertTrue(any('DOTNET_PROJECT_LOCK_RESOLVED_MISMATCH:' in item for item in blockers))

    def test_requested_range_must_bind_declared_project_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            write_lock(project)
            lock = project.parent / 'packages.lock.json'
            payload = json.loads(lock.read_text(encoding='utf-8'))
            payload['dependencies']['net10.0-windows7.0'][
                'Microsoft.Web.WebView2'
            ]['requested'] = '[0.0.0, )'
            lock.write_text(json.dumps(payload), encoding='utf-8')

            blockers = dotnet_lock_content_blockers(root, project)
            self.assertTrue(
                any(
                    item.startswith('DOTNET_PROJECT_LOCK_REQUESTED_MISMATCH:')
                    for item in blockers
                )
            )
            with self.assertRaisesRegex(ValueError, 'does not match project'):
                dotnet_locked_dependency_graph(root, [project])

    def test_content_hash_must_be_base64_sha512(self):
        bad_hashes = ('', 'not-base64', base64.b64encode(b'short').decode('ascii'), None)
        for value in bad_hashes:
            with self.subTest(value=value), TemporaryDirectory() as directory:
                root = Path(directory)
                project = write_project(root)
                write_lock(project, content_hash=value)
                blockers = dotnet_lock_content_blockers(root, project)
                self.assertTrue(any('DOTNET_PROJECT_LOCK_CONTENT_HASH_INVALID:' in item for item in blockers))

    def test_content_hash_must_use_canonical_base64_encoding(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            # 64 bytes encode with two pad characters.  Alter only the
            # unused low pad bits so Python's strict decoder still yields the
            # same 64 bytes while canonical base64 re-encoding differs.
            self.assertTrue(GOOD_HASH.endswith('w=='))
            noncanonical = GOOD_HASH[:-3] + 'x=='
            self.assertEqual(
                base64.b64decode(noncanonical, validate=True),
                base64.b64decode(GOOD_HASH, validate=True),
            )
            write_lock(project, content_hash=noncanonical)
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertTrue(
                any(
                    item.startswith('DOTNET_PROJECT_LOCK_CONTENT_HASH_INVALID:')
                    for item in blockers
                )
            )

    def test_package_name_case_drift_is_visible(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            write_lock(project, package_name='microsoft.web.webview2')
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertTrue(any('DOTNET_PROJECT_LOCK_NAME_CASE_MISMATCH:' in item for item in blockers))

    def test_duplicate_or_conflicting_project_reference_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root, duplicate=True)
            write_lock(project)
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertEqual(len(blockers), 1)
            self.assertIn('DOTNET_PROJECT_PACKAGE_REFERENCE_AMBIGUOUS:', blockers[0])

    def test_auto_referenced_direct_package_does_not_false_block(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            payload = {
                'version': 1,
                'dependencies': {
                    'net10.0-windows7.0': {
                        'Microsoft.Web.WebView2': {
                            'type': 'Direct', 'requested': '[1.0.4191.47, )',
                            'resolved': '1.0.4191.47', 'contentHash': GOOD_HASH,
                        },
                        'Microsoft.NET.ILLink.Tasks': {
                            'type': 'Direct', 'requested': '[10.0.0, )',
                            'resolved': '10.0.0', 'contentHash': GOOD_HASH,
                        },
                    }
                }
            }
            (project.parent / 'packages.lock.json').write_text(json.dumps(payload), encoding='utf-8')
            self.assertEqual(dotnet_lock_content_blockers(root, project), [])

    def test_same_direct_reference_can_exist_in_multiple_targets(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            record = {
                'Microsoft.Web.WebView2': {
                    'type': 'Direct', 'requested': '[1.0.4191.47, )',
                    'resolved': '1.0.4191.47', 'contentHash': GOOD_HASH,
                }
            }
            payload = {'version': 1, 'dependencies': {'net10.0-windows7.0': record, 'net10.0-windows10.0': record}}
            (project.parent / 'packages.lock.json').write_text(json.dumps(payload), encoding='utf-8')
            self.assertEqual(dotnet_lock_content_blockers(root, project), [])

    def test_release_graph_rejects_stale_direct_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            write_lock(project, resolved='1.0.4255-prerelease')
            with self.assertRaisesRegex(ValueError, 'does not match project'):
                dotnet_locked_dependency_graph(root, [project])

    def test_release_graph_binds_direct_and_transitive_hashes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            payload = {
                'version': 1,
                'dependencies': {
                    'net10.0-windows7.0': {
                        'Microsoft.Web.WebView2': {
                            'type': 'Direct', 'requested': '[1.0.4191.47, )',
                            'resolved': '1.0.4191.47', 'contentHash': GOOD_HASH,
                            'dependencies': {'Example.Transitive': '2.0.0'},
                        },
                        'Example.Transitive': {
                            'type': 'Transitive', 'resolved': '2.0.0', 'contentHash': GOOD_HASH,
                        },
                    }
                }
            }
            (project.parent / 'packages.lock.json').write_text(json.dumps(payload), encoding='utf-8')
            graph = dotnet_locked_dependency_graph(root, [project])
            self.assertEqual(len(graph), 2)
            self.assertEqual(graph[0]['name'], 'Example.Transitive')
            self.assertEqual(graph[0]['type'], 'Transitive')
            self.assertEqual(graph[1]['name'], 'Microsoft.Web.WebView2')
            self.assertEqual(graph[1]['version'], '1.0.4191.47')
            self.assertEqual(graph[1]['content_hash_sha512_base64'], GOOD_HASH)
            self.assertEqual(graph[1]['requested'], '[1.0.4191.47, )')

    def test_release_graph_hash_change_changes_manifest_input(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            write_lock(project)
            first = dotnet_locked_dependency_graph(root, [project])
            changed_hash = base64.b64encode(bytes(reversed(range(64)))).decode('ascii')
            write_lock(project, content_hash=changed_hash)
            second = dotnet_locked_dependency_graph(root, [project])
            self.assertNotEqual(first, second)

    def test_release_graph_rejects_transitive_without_content_hash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            payload = {
                'version': 1,
                'dependencies': {
                    'net10.0-windows7.0': {
                        'Microsoft.Web.WebView2': {
                            'type': 'Direct', 'requested': '[1.0.4191.47, )',
                            'resolved': '1.0.4191.47', 'contentHash': GOOD_HASH,
                        },
                        'Example.Transitive': {
                            'type': 'Transitive', 'resolved': '2.0.0', 'contentHash': 'bad',
                        },
                    }
                }
            }
            (project.parent / 'packages.lock.json').write_text(json.dumps(payload), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'invalid NuGet content hash'):
                dotnet_locked_dependency_graph(root, [project])

    def test_missing_package_version_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'src' / 'AutoTrade.Desktop' / 'AutoTrade.Desktop.csproj'
            project.parent.mkdir(parents=True)
            project.write_text(
                '<Project><ItemGroup><PackageReference Include="Microsoft.Web.WebView2" /></ItemGroup></Project>',
                encoding='utf-8',
            )
            write_lock(project)
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertEqual(len(blockers), 1)
            self.assertIn('DOTNET_PROJECT_PACKAGE_REFERENCE_AMBIGUOUS:', blockers[0])
            with self.assertRaisesRegex(ValueError, 'does not match project'):
                dotnet_locked_dependency_graph(root, [project])

    def test_missing_package_name_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'src' / 'AutoTrade.Desktop' / 'AutoTrade.Desktop.csproj'
            project.parent.mkdir(parents=True)
            project.write_text(
                '<Project><ItemGroup><PackageReference Version="1.0.4191.47" /></ItemGroup></Project>',
                encoding='utf-8',
            )
            write_lock(project)
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertEqual(len(blockers), 1)
            self.assertIn('DOTNET_PROJECT_PACKAGE_REFERENCE_AMBIGUOUS:', blockers[0])
            with self.assertRaisesRegex(ValueError, 'does not match project'):
                dotnet_locked_dependency_graph(root, [project])


    def test_transitive_hash_is_part_of_composition_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            payload = {
                'version': 1,
                'dependencies': {
                    'net10.0-windows7.0': {
                        'Microsoft.Web.WebView2': {
                            'type': 'Direct',
                            'requested': '[1.0.4191.47, )',
                            'resolved': '1.0.4191.47',
                            'contentHash': GOOD_HASH,
                        },
                        'Example.Transitive': {
                            'type': 'Transitive',
                            'resolved': '2.0.0',
                            'contentHash': 'bad',
                        },
                    }
                },
            }
            (project.parent / 'packages.lock.json').write_text(
                json.dumps(payload), encoding='utf-8'
            )
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertTrue(
                any(
                    item.startswith('DOTNET_PROJECT_LOCK_CONTENT_HASH_INVALID:')
                    and ':Example.Transitive:' in item
                    for item in blockers
                )
            )

    def test_auto_referenced_direct_still_requires_artifact_hash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            payload = {
                'version': 1,
                'dependencies': {
                    'net10.0-windows7.0': {
                        'Microsoft.Web.WebView2': {
                            'type': 'Direct',
                            'requested': '[1.0.4191.47, )',
                            'resolved': '1.0.4191.47',
                            'contentHash': GOOD_HASH,
                        },
                        'Microsoft.NET.ILLink.Tasks': {
                            'type': 'Direct',
                            'requested': '[10.0.0, )',
                            'resolved': '10.0.0',
                            'contentHash': 'bad',
                        },
                    }
                },
            }
            (project.parent / 'packages.lock.json').write_text(
                json.dumps(payload), encoding='utf-8'
            )
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertTrue(
                any(
                    item.startswith('DOTNET_PROJECT_LOCK_CONTENT_HASH_INVALID:')
                    and ':Microsoft.NET.ILLink.Tasks:' in item
                    for item in blockers
                )
            )

    def test_case_variant_package_ids_in_one_target_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            payload = {
                'version': 1,
                'dependencies': {
                    'net10.0-windows7.0': {
                        'Microsoft.Web.WebView2': {
                            'type': 'Direct',
                            'requested': '[1.0.4191.47, )',
                            'resolved': '1.0.4191.47',
                            'contentHash': GOOD_HASH,
                        },
                        'Example.Transitive': {
                            'type': 'Transitive',
                            'resolved': '2.0.0',
                            'contentHash': GOOD_HASH,
                        },
                        'example.transitive': {
                            'type': 'Transitive',
                            'resolved': '2.0.0',
                            'contentHash': GOOD_HASH,
                        },
                    }
                },
            }
            (project.parent / 'packages.lock.json').write_text(
                json.dumps(payload), encoding='utf-8'
            )
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertTrue(
                any(
                    item.startswith('DOTNET_PROJECT_LOCK_PACKAGE_CASE_AMBIGUOUS:')
                    for item in blockers
                )
            )
            with self.assertRaisesRegex(ValueError, 'does not match project'):
                dotnet_locked_dependency_graph(root, [project])

    def test_release_graph_binds_dependency_edges(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            payload = {
                'version': 1,
                'dependencies': {
                    'net10.0-windows7.0': {
                        'Microsoft.Web.WebView2': {
                            'type': 'Direct',
                            'requested': '[1.0.4191.47, )',
                            'resolved': '1.0.4191.47',
                            'contentHash': GOOD_HASH,
                            'dependencies': {
                                'Example.Transitive': '[2.0.0, )',
                            },
                        },
                        'Example.Transitive': {
                            'type': 'Transitive',
                            'resolved': '2.0.0',
                            'contentHash': GOOD_HASH,
                        },
                    }
                },
            }
            lock = project.parent / 'packages.lock.json'
            lock.write_text(json.dumps(payload), encoding='utf-8')
            first = dotnet_locked_dependency_graph(root, [project])
            direct = next(
                item for item in first
                if item['name'] == 'Microsoft.Web.WebView2'
            )
            self.assertEqual(
                direct['dependencies'],
                [{'name': 'Example.Transitive', 'requested': '[2.0.0, )'}],
            )

            payload['dependencies']['net10.0-windows7.0'][
                'Microsoft.Web.WebView2'
            ]['dependencies']['Example.Transitive'] = '[1.0.0, )'
            lock.write_text(json.dumps(payload), encoding='utf-8')
            second = dotnet_locked_dependency_graph(root, [project])
            self.assertNotEqual(first, second)

    def test_dependency_edge_target_must_exist_in_same_lock_target(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            payload = {
                'version': 1,
                'dependencies': {
                    'net10.0-windows7.0': {
                        'Microsoft.Web.WebView2': {
                            'type': 'Direct',
                            'requested': '[1.0.4191.47, )',
                            'resolved': '1.0.4191.47',
                            'contentHash': GOOD_HASH,
                            'dependencies': {
                                'Missing.Transitive': '[2.0.0, )',
                            },
                        },
                    }
                },
            }
            (project.parent / 'packages.lock.json').write_text(
                json.dumps(payload), encoding='utf-8'
            )
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertTrue(
                any(
                    item.startswith(
                        'DOTNET_PROJECT_LOCK_DEPENDENCY_TARGET_MISSING:'
                    )
                    for item in blockers
                )
            )
            with self.assertRaisesRegex(ValueError, 'does not match project'):
                dotnet_locked_dependency_graph(root, [project])

    def test_dependency_edge_case_must_match_target_record_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            payload = {
                'version': 1,
                'dependencies': {
                    'net10.0-windows7.0': {
                        'Microsoft.Web.WebView2': {
                            'type': 'Direct',
                            'requested': '[1.0.4191.47, )',
                            'resolved': '1.0.4191.47',
                            'contentHash': GOOD_HASH,
                            'dependencies': {
                                'example.transitive': '[2.0.0, )',
                            },
                        },
                        'Example.Transitive': {
                            'type': 'Transitive',
                            'resolved': '2.0.0',
                            'contentHash': GOOD_HASH,
                        },
                    }
                },
            }
            (project.parent / 'packages.lock.json').write_text(
                json.dumps(payload), encoding='utf-8'
            )
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertTrue(
                any(
                    item.startswith(
                        'DOTNET_PROJECT_LOCK_DEPENDENCY_EDGE_CASE_MISMATCH:'
                    )
                    for item in blockers
                )
            )

    def test_invalid_dependency_edge_shape_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            payload = {
                'version': 1,
                'dependencies': {
                    'net10.0-windows7.0': {
                        'Microsoft.Web.WebView2': {
                            'type': 'Direct',
                            'requested': '[1.0.4191.47, )',
                            'resolved': '1.0.4191.47',
                            'contentHash': GOOD_HASH,
                            'dependencies': ['not-a-map'],
                        },
                    }
                },
            }
            (project.parent / 'packages.lock.json').write_text(
                json.dumps(payload), encoding='utf-8'
            )
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertTrue(
                any(
                    item.startswith(
                        'DOTNET_PROJECT_LOCK_DEPENDENCY_EDGES_INVALID:'
                    )
                    for item in blockers
                )
            )


    def test_namespaced_project_package_reference_cannot_bypass_lock_gate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'src' / 'AutoTrade.Desktop' / 'AutoTrade.Desktop.csproj'
            project.parent.mkdir(parents=True)
            project.write_text(
                '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
                '<ItemGroup><PackageReference Include="Microsoft.Web.WebView2" '
                'Version="1.0.4191.47" /></ItemGroup></Project>',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_project_package_references(project),
                [('Microsoft.Web.WebView2', '1.0.4191.47')],
            )
            blockers = dotnet_lock_content_blockers(root, project)
            self.assertEqual(
                blockers,
                ['DOTNET_PROJECT_LOCK_MISSING:src/AutoTrade.Desktop/AutoTrade.Desktop.csproj'],
            )

    def test_third_party_project_sdk_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'src' / 'PluginHost' / 'PluginHost.csproj'
            project.parent.mkdir(parents=True)
            project.write_text(
                '<Project Sdk="Third.Party.Sdk/1.2.3" />',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                [
                    'DOTNET_PROJECT_SDK_AUTHORITY_UNSUPPORTED:'
                    'src/PluginHost/PluginHost.csproj:Third.Party.Sdk/1.2.3'
                ],
            )

    def test_child_sdk_declaration_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'src' / 'PluginHost' / 'PluginHost.csproj'
            project.parent.mkdir(parents=True)
            project.write_text(
                '<Project><Sdk Name="Third.Party.Sdk" Version="1.2.3" /></Project>',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                [
                    'DOTNET_PROJECT_SDK_ELEMENT_UNSUPPORTED:'
                    'src/PluginHost/PluginHost.csproj'
                ],
            )

    def test_canonical_microsoft_dotnet_sdk_is_not_blocked(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'src' / 'App' / 'App.csproj'
            project.parent.mkdir(parents=True)
            project.write_text(
                '<Project Sdk="Microsoft.NET.Sdk" />',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                [],
            )

    def test_explicit_project_import_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'src' / 'AutoTrade.Desktop' / 'AutoTrade.Desktop.csproj'
            imported = root / 'external' / 'Injected.targets'
            project.parent.mkdir(parents=True)
            imported.parent.mkdir(parents=True)
            imported.write_text(
                '<Project><ItemGroup><PackageReference Include="Injected.Package" '
                'Version="9.9.9" /></ItemGroup></Project>',
                encoding='utf-8',
            )
            project.write_text(
                '<Project>'
                '<Import Project="../../external/Injected.targets" />'
                '<ItemGroup><PackageReference Include="Microsoft.Web.WebView2" '
                'Version="1.0.4191.47" /></ItemGroup>'
                '</Project>',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                [
                    'DOTNET_EXPLICIT_MSBUILD_IMPORT_UNSUPPORTED:'
                    'src/AutoTrade.Desktop/AutoTrade.Desktop.csproj'
                ],
            )
            with self.assertRaisesRegex(
                ValueError,
                'imported MSBuild PackageReference',
            ):
                dotnet_locked_dependency_graph(root, [project])

    def test_restore_authority_property_names_are_case_insensitive(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'src' / 'App' / 'App.csproj'
            project.parent.mkdir(parents=True)
            project.write_text(
                '<Project TreatAsLocalProperty="restorelockedmode">'
                '<PropertyGroup>'
                '<restoreforceevaluate>true</restoreforceevaluate>'
                '</PropertyGroup></Project>',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                [
                    'DOTNET_RESTORE_AUTHORITY_PROPERTY_UNSUPPORTED:'
                    'src/App/App.csproj:RestoreForceEvaluate',
                    'DOTNET_RESTORE_AUTHORITY_LOCAL_OVERRIDE_UNSUPPORTED:'
                    'src/App/App.csproj:RestoreLockedMode',
                ],
            )

    def test_project_cannot_localize_restore_locked_mode(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'src' / 'AutoTrade.Desktop' / 'AutoTrade.Desktop.csproj'
            project.parent.mkdir(parents=True)
            project.write_text(
                '<Project TreatAsLocalProperty="RestoreLockedMode">'
                '<PropertyGroup><RestoreLockedMode>false</RestoreLockedMode>'
                '</PropertyGroup><ItemGroup>'
                '<PackageReference Include="Microsoft.Web.WebView2" '
                'Version="1.0.4191.47" />'
                '</ItemGroup></Project>',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                [
                    'DOTNET_RESTORE_AUTHORITY_LOCAL_OVERRIDE_UNSUPPORTED:'
                    'src/AutoTrade.Desktop/AutoTrade.Desktop.csproj:'
                    'RestoreLockedMode'
                ],
            )

    def test_props_cannot_localize_restore_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            props = root / 'Directory.Build.props'
            props.write_text(
                '<Project TreatAsLocalProperty="Other; RestoreForceEvaluate">'
                '<PropertyGroup><Other>value</Other></PropertyGroup>'
                '</Project>',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                [
                    'DOTNET_RESTORE_AUTHORITY_LOCAL_OVERRIDE_UNSUPPORTED:'
                    'Directory.Build.props:RestoreForceEvaluate'
                ],
            )

    def test_project_restore_force_evaluate_property_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'src' / 'AutoTrade.Desktop' / 'AutoTrade.Desktop.csproj'
            project.parent.mkdir(parents=True)
            project.write_text(
                '<Project><PropertyGroup>'
                '<RestoreForceEvaluate>true</RestoreForceEvaluate>'
                '</PropertyGroup><ItemGroup>'
                '<PackageReference Include="Microsoft.Web.WebView2" '
                'Version="1.0.4191.47" />'
                '</ItemGroup></Project>',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                [
                    'DOTNET_RESTORE_AUTHORITY_PROPERTY_UNSUPPORTED:'
                    'src/AutoTrade.Desktop/AutoTrade.Desktop.csproj:'
                    'RestoreForceEvaluate'
                ],
            )

    def test_props_custom_lock_path_property_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            props = root / 'Directory.Build.props'
            props.write_text(
                '<Project><PropertyGroup>'
                '<NuGetLockFilePath>artifacts/other.lock.json</NuGetLockFilePath>'
                '</PropertyGroup></Project>',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                [
                    'DOTNET_RESTORE_AUTHORITY_PROPERTY_UNSUPPORTED:'
                    'Directory.Build.props:NuGetLockFilePath'
                ],
            )
            with self.assertRaisesRegex(
                ValueError,
                'restore authority',
            ):
                dotnet_locked_dependency_graph(root, [])

    def test_root_props_package_reference_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            props = root / 'Directory.Build.props'
            props.write_text(
                '<Project><ItemGroup><PackageReference Include="Injected.Package" '
                'Version="1.2.3" /></ItemGroup></Project>',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                ['DOTNET_IMPORTED_PACKAGE_REFERENCE_UNSUPPORTED:Directory.Build.props'],
            )
            with self.assertRaisesRegex(
                ValueError,
                'imported MSBuild PackageReference',
            ):
                dotnet_locked_dependency_graph(root, [])

    def test_namespaced_src_props_package_reference_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            props = root / 'src' / 'Build' / 'Dependencies.props'
            props.parent.mkdir(parents=True)
            props.write_text(
                '<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
                '<ItemGroup><PackageReference Include="Injected.Package" '
                'Version="1.2.3" /></ItemGroup></Project>',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                ['DOTNET_IMPORTED_PACKAGE_REFERENCE_UNSUPPORTED:src/Build/Dependencies.props'],
            )

    def test_nested_props_import_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            imported = root / 'external' / 'Injected.targets'
            imported.parent.mkdir(parents=True)
            imported.write_text(
                '<Project><ItemGroup><PackageReference Include="Injected.Package" '
                'Version="9.9.9" /></ItemGroup></Project>',
                encoding='utf-8',
            )
            props = root / 'Directory.Build.props'
            props.write_text(
                '<Project><Import Project="external/Injected.targets" /></Project>',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                ['DOTNET_EXPLICIT_MSBUILD_IMPORT_UNSUPPORTED:Directory.Build.props'],
            )

    def test_malformed_root_props_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Directory.Build.props').write_text(
                '<Project><ItemGroup>',
                encoding='utf-8',
            )
            self.assertEqual(
                dotnet_imported_package_reference_blockers(root),
                ['DOTNET_MSBUILD_DEPENDENCY_SOURCE_INVALID:Directory.Build.props'],
            )


if __name__ == '__main__':
    unittest.main()
