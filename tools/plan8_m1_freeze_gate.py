"""Plan-8 M1 provider-free same-run package/evidence freeze verification.

Verifier of existing CI artifacts, NOT a release signer, provider issuer,
Windows authority, financial sender or trading gate. Only actual successful
exact-source CI may produce an internal non-release receipt.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import stat
import subprocess
from zipfile import ZipFile, BadZipFile

GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
REQUIRED = {
    "browser": ("provider-free-whole-product-browser", "Linux"),
    "packaged": ("provider-free-packaged-windows-browser", "Windows"),
    "recovery": ("plan8-m1-s4-crash-restore-financial-conservation", "Linux"),
    "surface": ("plan8-m1-s5-web-desktop-update-load", "Linux"),
    "composed": ("plan8-m1-s5-composed-engineering", "Linux"),
}


class M1FreezeError(ValueError):
    pass


def _file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise M1FreezeError("required ordinary file is missing or is an alias")
    return path.read_bytes()


def _json(data: bytes, *, label: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise M1FreezeError(label + ": duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(M1FreezeError("nonfinite JSON")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise M1FreezeError(label + ": invalid JSON") from exc


def freeze(*, source_sha: str, tree_sha: str, run_id: str, run_attempt: str,
           windows: Path, browser: Path, recovery: Path, surface: Path, composed: Path) -> dict:
    if not isinstance(source_sha, str) or not GIT_SHA.fullmatch(source_sha):
        raise M1FreezeError("source SHA must be exact lowercase git SHA")
    if not isinstance(tree_sha, str) or not GIT_SHA.fullmatch(tree_sha):
        raise M1FreezeError("tree SHA must be exact lowercase git SHA")
    if not isinstance(run_id, str) or not run_id.isdecimal() or int(run_id) <= 0:
        raise M1FreezeError("invalid run id")
    if not isinstance(run_attempt, str) or not run_attempt.isdecimal() or int(run_attempt) <= 0:
        raise M1FreezeError("invalid run attempt")
    windows = Path(windows)
    bundle_path = windows / "AutoTrade-ZERO-win-x64.zip"
    bundle = _file(bundle_path)
    digest = sha256(bundle).hexdigest()
    sidecar = _file(windows / "AutoTrade-ZERO-win-x64.zip.sha256")
    if sidecar != f"{digest}  {bundle_path.name}\n".encode("ascii"):
        raise M1FreezeError("Windows bundle hash sidecar mismatch")
    result = _json(_file(windows / "candidate-work" / "candidate-result.json"), label="candidate")
    if (type(result) is not dict or result.get("source_sha") != source_sha
            or result.get("package_sha256") != digest or result.get("release_eligible") is not False
            or result.get("nvda_verified") is not False):
        raise M1FreezeError("candidate source, package digest or nonrelease claims mismatch")
    try:
        from io import BytesIO
        with ZipFile(BytesIO(bundle)) as archive:
            names = archive.namelist()
            if names.count("bundle-manifest.json") != 1 or len(names) != len(set(names)):
                raise M1FreezeError("missing or ambiguous bundle manifest")
            manifest = _json(archive.read("bundle-manifest.json"), label="bundle manifest")
    except (BadZipFile, OSError, KeyError) as exc:
        raise M1FreezeError("invalid package ZIP") from exc
    if (type(manifest) is not dict or manifest.get("source_sha") != source_sha
            or manifest.get("mode") != "diagnostics"
            or manifest.get("release_eligible") is not False
            or manifest.get("trading_authority_granted_by_artifact") is not False):
        raise M1FreezeError("bundle manifest grants unsupported release or trade authority")
    paths = {"browser": browser, "packaged": windows / "product-packaged-Windows.json",
             "recovery": recovery, "surface": surface, "composed": composed}
    bound = {}
    for role, (suite, expected_os) in REQUIRED.items():
        raw = _file(Path(paths[role]))
        evidence = _json(raw, label=role)
        github = evidence.get("github") if type(evidence) is dict else None
        if (type(evidence) is not dict or evidence.get("schema_version") != "1.0.0"
                or evidence.get("source_sha") != source_sha
                or evidence.get("checked_out_sha") != source_sha
                or evidence.get("suite") != suite or evidence.get("result") != "PASS"
                or evidence.get("runner_os") != expected_os
                or evidence.get("contains_secrets") is not False
                or type(github) is not dict or github.get("workflow") != "provider-free-product"
                or github.get("run_id") != run_id or github.get("run_attempt") != run_attempt
                or github.get("event_name") not in ("pull_request", "push", "workflow_dispatch")):
            raise M1FreezeError("evidence role/source/suite/run/OS mismatch: " + role)
        bound[role] = {"suite": suite, "sha256": "sha256:" + sha256(raw).hexdigest()}
    return {"schema_version": "1.0.0", "evidence_class": "EXACT_SOURCE_PROVIDER_FREE_CI",
            "source_sha": source_sha, "tree_sha": tree_sha,
            "github_run_id": run_id, "github_run_attempt": run_attempt,
            "package_sha256": "sha256:" + digest, "evidence": bound,
            "release_eligible": False, "nvda_verified": False,
            "real_provider_qualified": False, "paper_or_live_qualified": False,
            "claimed_profitability": False, "financial_authority_granted": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("source-sha", "run-id", "run-attempt", "windows", "browser", "recovery", "surface", "composed", "output"):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args()
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if actual != args.source_sha:
        raise M1FreezeError("checkout does not match frozen source")
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], text=True).strip()
    record = freeze(source_sha=args.source_sha, tree_sha=tree,
                    run_id=args.run_id, run_attempt=args.run_attempt,
                    windows=Path(args.windows), browser=Path(args.browser),
                    recovery=Path(args.recovery), surface=Path(args.surface),
                    composed=Path(args.composed))
    from research.autotrade_research.artifacts.durable_publish import atomic_write_json
    atomic_write_json(Path(args.output), record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
