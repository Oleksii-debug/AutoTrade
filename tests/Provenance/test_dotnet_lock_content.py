from __future__ import annotations

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.dotnet_lock import dotnet_lock_content_blockers, dotnet_locked_dependency_graph


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

    def test_unsupported_lock_schema_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project = write_project(root)
            write_lock(project, version=2)
            self.assertEqual(
                dotnet_lock_content_blockers(root, project),
                ['DOTNET_PROJECT_LOCK_VERSION_UNSUPPORTED:src/AutoTrade.Desktop/AutoTrade.Desktop.csproj:2'],
            )

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

    def test_content_hash_must_be_base64_sha512(self):
        bad_hashes = ('', 'not-base64', base64.b64encode(b'short').decode('ascii'), None)
        for value in bad_hashes:
            with self.subTest(value=value), TemporaryDirectory() as directory:
                root = Path(directory)
                project = write_project(root)
                write_lock(project, content_hash=value)
                blockers = dotnet_lock_content_blockers(root, project)
                self.assertTrue(any('DOTNET_PROJECT_LOCK_CONTENT_HASH_INVALID:' in item for item in blockers))

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


if __name__ == '__main__':
    unittest.main()
