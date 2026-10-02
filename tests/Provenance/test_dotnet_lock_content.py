from __future__ import annotations

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.dotnet_lock import dotnet_lock_content_blockers, normalized_dotnet_lock
from tools.build_provenance_manifest import _normalized_dotnet_lock as manifest_normalized_dotnet_lock

VALID_HASH = base64.b64encode(bytes(range(64))).decode("ascii")


class DotnetLockTests(unittest.TestCase):
    def _project(self, root: Path, *, name="Pkg", requested="[1.2.3]") -> Path:
        project = root / "src" / "App" / "App.csproj"
        project.parent.mkdir(parents=True)
        project.write_text(
            f'<Project><ItemGroup><PackageReference Include="{name}" Version="{requested}" /></ItemGroup></Project>\n',
            encoding="utf-8",
        )
        return project

    def _lock(self, project: Path, record: dict, *, package="Pkg") -> None:
        (project.parent / "packages.lock.json").write_text(
            json.dumps({"version": 1, "dependencies": {"net10.0": {package: record}}}) + "\n",
            encoding="utf-8",
        )

    def test_exact_direct_package_and_hash_pass(self):
        with TemporaryDirectory() as d:
            root=Path(d); project=self._project(root)
            self._lock(project,{"type":"Direct","requested":"[1.2.3]","resolved":"1.2.3","contentHash":VALID_HASH})
            self.assertEqual(dotnet_lock_content_blockers(root,project),[])
            lock_path = project.parent/"packages.lock.json"
            self.assertEqual(normalized_dotnet_lock(lock_path)["version"],1)
            self.assertEqual(manifest_normalized_dotnet_lock(lock_path), normalized_dotnet_lock(lock_path))

    def test_invalid_hash_fails(self):
        with TemporaryDirectory() as d:
            root=Path(d); project=self._project(root)
            self._lock(project,{"type":"Direct","requested":"[1.2.3]","resolved":"1.2.3","contentHash":"abc"})
            self.assertTrue(dotnet_lock_content_blockers(root,project)[0].startswith("DOTNET_PROJECT_LOCK_CONTENT_INVALID:"))

    def test_duplicate_json_key_fails(self):
        with TemporaryDirectory() as d:
            root=Path(d); project=self._project(root); p=project.parent/"packages.lock.json"
            p.write_text('{"version":1,"version":1,"dependencies":{"net10.0":{}}}\n',encoding="utf-8")
            self.assertTrue(dotnet_lock_content_blockers(root,project)[0].startswith("DOTNET_PROJECT_LOCK_CONTENT_INVALID:"))

    def test_transitive_cannot_satisfy_direct(self):
        with TemporaryDirectory() as d:
            root=Path(d); project=self._project(root)
            self._lock(project,{"type":"Transitive","resolved":"1.2.3","contentHash":VALID_HASH})
            self.assertIn("DOTNET_PROJECT_LOCK_DIRECT_MISSING:src/App/App.csproj:Pkg@[1.2.3]",dotnet_lock_content_blockers(root,project))

    def test_requested_and_resolved_must_match(self):
        with TemporaryDirectory() as d:
            root=Path(d); project=self._project(root)
            self._lock(project,{"type":"Direct","requested":"[1.2.4]","resolved":"1.2.4","contentHash":VALID_HASH})
            blockers=dotnet_lock_content_blockers(root,project)
            self.assertTrue(any("REQUESTED_MISMATCH" in x for x in blockers))
            self.assertTrue(any("RESOLVED_MISMATCH" in x for x in blockers))

    def test_case_drift_is_visible(self):
        with TemporaryDirectory() as d:
            root=Path(d); project=self._project(root,name="Microsoft.Web.WebView2")
            self._lock(project,{"type":"Direct","requested":"[1.2.3]","resolved":"1.2.3","contentHash":VALID_HASH},package="microsoft.web.webview2")
            self.assertTrue(any("NAME_CASE_MISMATCH" in x for x in dotnet_lock_content_blockers(root,project)))

    def test_project_record_cannot_claim_package_bytes(self):
        with TemporaryDirectory() as d:
            root=Path(d); project=self._project(root)
            self._lock(project,{"type":"Project","resolved":"1.2.3","contentHash":VALID_HASH})
            self.assertTrue(dotnet_lock_content_blockers(root,project)[0].startswith("DOTNET_PROJECT_LOCK_CONTENT_INVALID:"))

    def test_empty_target_lock_is_valid_for_project_without_packages(self):
        with TemporaryDirectory() as d:
            root=Path(d); project=root/"src"/"Contracts"/"Contracts.csproj"; project.parent.mkdir(parents=True)
            project.write_text("<Project />\n",encoding="utf-8")
            p=project.parent/"packages.lock.json"; p.write_text('{"version":1,"dependencies":{"net10.0":{}}}\n',encoding="utf-8")
            self.assertEqual(normalized_dotnet_lock(p),{"version":1,"dependencies":{"net10.0":{}}})


if __name__ == "__main__":
    unittest.main()
