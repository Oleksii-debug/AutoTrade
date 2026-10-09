"""Synthetic negatives for Plan-8 same-run M1 freeze receipt (not release)."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile
from hashlib import sha256

from tools.plan8_m1_freeze_gate import freeze, M1FreezeError, REQUIRED

SHA = "a" * 40
TREE = "b" * 40


class M1FreezeGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.windows = self.root / "windows"
        (self.windows / "candidate-work").mkdir(parents=True)
        bundle = self.windows / "AutoTrade-ZERO-win-x64.zip"
        with ZipFile(bundle, "w") as z:
            z.writestr("bundle-manifest.json", json.dumps({
                "source_sha": SHA, "mode": "diagnostics", "release_eligible": False,
                "trading_authority_granted_by_artifact": False}))
        self.digest = sha256(bundle.read_bytes()).hexdigest()
        (self.windows / (bundle.name + ".sha256")).write_text(
            f"{self.digest}  {bundle.name}\n")
        (self.windows / "candidate-work" / "candidate-result.json").write_text(json.dumps({
            "source_sha": SHA, "package_sha256": self.digest,
            "release_eligible": False, "nvda_verified": False}))
        self.files = {}
        for role, (suite, runner_os) in REQUIRED.items():
            path = (self.windows / "product-packaged-Windows.json") if role == "packaged" else self.root / (role + ".json")
            path.write_text(json.dumps({
                "schema_version": "1.0.0", "source_sha": SHA, "checked_out_sha": SHA,
                "suite": suite, "result": "PASS", "runner_os": runner_os,
                "contains_secrets": False, "github": {"workflow": "provider-free-product",
                  "event_name": "pull_request", "run_id": "42", "run_attempt": "1"}}))
            self.files[role] = path

    def do_freeze(self):
        return freeze(source_sha=SHA, tree_sha=TREE, run_id="42", run_attempt="1",
                      windows=self.windows, browser=self.files["browser"],
                      recovery=self.files["recovery"], surface=self.files["surface"],
                      composed=self.files["composed"])

    def test_exact_matching_nonrelease_receipt(self):
        receipt = self.do_freeze()
        self.assertEqual(receipt["source_sha"], SHA)
        self.assertEqual(receipt["package_sha256"], "sha256:" + self.digest)
        self.assertEqual(len(receipt["evidence"]), 5)
        self.assertFalse(receipt["release_eligible"])
        self.assertFalse(receipt["financial_authority_granted"])

    def test_changed_bundle_fails_before_receipt(self):
        with (self.windows / "AutoTrade-ZERO-win-x64.zip").open("ab") as f:
            f.write(b"tampered")
        with self.assertRaisesRegex(M1FreezeError, "sidecar mismatch"):
            self.do_freeze()

    def test_wrong_candidate_source_or_release_claim_fails(self):
        p = self.windows / "candidate-work" / "candidate-result.json"
        for change in ({"source_sha": "c" * 40}, {"release_eligible": True},
                       {"nvda_verified": True}):
            before = p.read_text()
            data = json.loads(before); data.update(change); p.write_text(json.dumps(data))
            with self.subTest(change=change), self.assertRaises(M1FreezeError):
                self.do_freeze()
            p.write_text(before)

    def test_stale_or_forged_execution_evidence_fails(self):
        p = self.files["recovery"]
        for change in ({"result": "QUEUED"}, {"source_sha": "d" * 40},
                       {"suite": "wrong"}, {"runner_os": "Windows"},
                       {"contains_secrets": True}):
            before = p.read_text(); data = json.loads(before); data.update(change)
            p.write_text(json.dumps(data))
            with self.subTest(change=change), self.assertRaises(M1FreezeError):
                self.do_freeze()
            p.write_text(before)

    def test_cross_run_and_fake_release_evidence_fail(self):
        p = self.files["composed"]; before = p.read_text()
        for changes in ({"run_id": "other"}, {"run_attempt": "2"},
                        {"workflow": "Verify AutoTrade"}, {"event_name": "unknown"}):
            data = json.loads(before); data["github"].update(changes)
            p.write_text(json.dumps(data))
            with self.subTest(changes=changes), self.assertRaises(M1FreezeError):
                self.do_freeze()
        p.write_text(before)

    def test_duplicate_json_key_and_symlink_fails_closed(self):
        p = self.files["surface"]; before = p.read_text()
        p.write_text(before.replace('"result": "PASS"', '"result": "PASS", "result": "PASS"'))
        with self.assertRaisesRegex(M1FreezeError, "duplicate JSON"):
            self.do_freeze()
        p.write_text(before)
        p.unlink(); p.symlink_to(self.files["browser"])
        with self.assertRaisesRegex(M1FreezeError, "alias"):
            self.do_freeze()


if __name__ == "__main__":
    unittest.main()
