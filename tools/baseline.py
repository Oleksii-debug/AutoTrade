"""Validate canonical baseline and export an exact committed snapshot (stdlib only)."""
from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLAN = "docs/engineering/08_DELIVERY_DEPENDENCY_AND_WORK_PACKAGE_PLAN.md"
BANK = "control/work-packages/bank.json"
MARKER = "## 5. Work-package bank"


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def render_bank(bank: dict) -> str:
    lines = [MARKER, "", "Generated from `control/work-packages/bank.json` by `python tools/baseline.py refresh`. Edit that bank, then refresh; status is evidence-bound.", ""]
    labels = {
        "authority_family": "Authority family", "exact_scope": "Exact scope",
        "required_inputs": "Inputs", "affected_contracts": "Contracts",
        "likely_files_modules": "Likely modules", "dependencies": "Dependencies",
        "reusable_code_sources": "Reuse sources", "tests": "Tests",
        "acceptance_criteria": "Acceptance", "integration_target": "Integration target",
        "forbidden_scope": "Forbidden scope", "conflicts_to_avoid": "Conflicts",
        "proposed_status": "Status", "status_note": "Status evidence / remaining work",
    }
    for package in bank["packages"]:
        lines += [f"### {package['id']} — {package['semantic_responsibility']}", ""]
        for key, label in labels.items():
            value = package[key]
            if isinstance(value, list):
                value = "; ".join(value) or "None (approved baseline required)"
            lines.append(f"- **{label}:** {value}")
        lines.append("")
    return "\n".join(lines) + "\n"


def refresh(root: Path = ROOT) -> None:
    bank = json.loads((root / BANK).read_text(encoding="utf-8"))
    path = root / PLAN
    prefix = path.read_text(encoding="utf-8").split(MARKER, 1)[0]
    path.write_text(prefix + render_bank(bank), encoding="utf-8", newline="\n")


def check(root: Path = ROOT) -> dict:
    errors = []
    bank = json.loads((root / BANK).read_text(encoding="utf-8"))
    packages = bank["packages"]
    by_id = {p["id"]: p for p in packages}
    if len(by_id) != len(packages) or set(by_id) != {f"WP-{i:02}" for i in range(1, 66)}:
        errors.append("work-package IDs must be exactly WP-01..WP-65 without duplicates")
    active, visited = set(), set()
    def visit(key):
        if key in active:
            raise ValueError(f"dependency cycle at {key}")
        if key in visited:
            return
        active.add(key)
        for dep in by_id[key]["dependencies"]:
            if dep not in by_id:
                raise ValueError(f"missing dependency {dep}")
            visit(dep)
        active.remove(key)
        visited.add(key)
    for key in by_id:
        visit(key)
    plan = (root / PLAN).read_text(encoding="utf-8")
    if MARKER + plan.split(MARKER, 1)[1] != render_bank(bank):
        errors.append("document 08 drift: run python tools/baseline.py refresh")
    index = json.loads((root / "control/INDEX.json").read_text(encoding="utf-8"))
    def check_paths(value):
        if isinstance(value, dict):
            for key, v in value.items():
                if key != "canonical_precedence":
                    check_paths(v)
        elif isinstance(value, list):
            for v in value:
                check_paths(v)
        elif isinstance(value, str) and value.startswith(("docs/", "control/", "tools/", "provenance/", "tests/")):
            if value == "control/registry":  # Branch, not filesystem path.
                return
            if not (root / value).exists():
                errors.append(f"INDEX target missing: {value}")
    check_paths(index)
    components = json.loads((root / "provenance/components.json").read_text(encoding="utf-8"))["components"]
    lean = next(c for c in components if c["repository"] == "QuantConnect/Lean")
    if lean.get("revision_type") != "commit" or lean.get("tree") != "4b163abf9fca60e731b76510b9ae6721ffff7e6c":
        errors.append("LEAN commit/tree provenance missing or inconsistent")
    for number in ("00", "01", "09"):
        text = next((root / "docs/engineering").glob(number + "_*.md")).read_text(encoding="utf-8")
        if lean["revision"] not in text or "reviewed source-tree object" in text or "must resolve and record the associated source commit" in text:
            errors.append(f"LEAN identity drift in document {number}")
    for folder in ("contracts", "control", "provenance"):
        for path in (root / folder).rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))
    if errors:
        raise ValueError("\n".join(errors))
    return {"work_packages": len(packages), "dependency_graph": "acyclic", "index_targets": "valid", "generated_plan": "current"}


def inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)


def markdown(text: str) -> str:
    out, paragraph = [], []
    listing = None
    table = fence = False
    def flush():
        if paragraph:
            out.append("<p>" + inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()
    for line in text.splitlines() + [""]:
        match = re.match(r"^(?:[-*] |(\d+)\. )(.*)", line)
        if listing and (not match or line.startswith("```")):
            out.append(f"</{listing}>")
            listing = None
        if table and not line.startswith("|"):
            out.append("</tbody></table></div>")
            table = False
        if line.startswith("```"):
            flush()
            fence = not fence
            out.append("<pre><code>" if fence else "</code></pre>")
        elif fence:
            out.append(html.escape(line) + "\n")
        elif line.startswith("|"):
            flush()
            if re.fullmatch(r"[|:\s-]+", line):
                continue
            cells = [inline(c.strip()) for c in line.strip("|").split("|")]
            if not table:
                out.append('<div class="tablewrap"><table><thead><tr>' + "".join('<th scope="col">' + c + "</th>" for c in cells) + "</tr></thead><tbody>")
                table = True
            else:
                out.append("<tr>" + "".join("<td>" + c + "</td>" for c in cells) + "</tr>")
        elif match:
            flush()
            kind = "ol" if match[1] else "ul"
            if listing != kind:
                if listing:
                    out.append(f"</{listing}>")
                out.append(f"<{kind}>")
                listing = kind
            out.append("<li>" + inline(match[2]) + "</li>")
        elif heading := re.match(r"^(#{1,6}) (.*)", line):
            flush()
            level = min(len(heading[1]) + 1, 6)
            out.append(f"<h{level}>" + inline(heading[2]) + f"</h{level}>")
        elif not line.strip():
            flush()
        else:
            paragraph.append(line)
    return "\n".join(out)


def reading_edition(root: Path) -> str:
    docs = sorted((root / "docs/engineering").glob("[0-9][0-9]_*.md"))
    nav = '<nav aria-label="Зміст"><h2>Зміст</h2><ol>' + "".join(f'<li><a href="#doc-{i}">{html.escape(p.stem)}</a></li>' for i, p in enumerate(docs)) + "</ol></nav>"
    body = '<main id="main"><h1>AutoTrade — узгоджений інженерний пакет</h1><p>Точна ревізія в SOURCE_REVISION.json. Первинні документи та код збережені в архіві. Банк робіт: control/work-packages/bank.json. GitHub містить подальші зміни.</p>' + nav
    for i, path in enumerate(docs):
        body += f'<section lang="en" id="doc-{i}">' + markdown(path.read_text(encoding="utf-8")) + '<p><a href="#main">До змісту</a></p></section>'
    return '<!doctype html><html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>AutoTrade baseline</title><style>body{max-width:1100px;margin:auto;padding:24px;font:18px/1.6 system-ui;color:#17202a;background:white}a{color:#074d9c}a:focus{outline:3px solid #a64500}section{border-top:2px solid #ccd4de;margin-top:2rem}table{border-collapse:collapse}th,td{border:1px solid #8797a8;padding:.6em;text-align:left;vertical-align:top}.tablewrap{overflow-x:auto}pre{white-space:pre-wrap}code{overflow-wrap:anywhere}</style></head><body><a href="#main">До основного вмісту</a>' + body + '</main></body></html>'


class LinkCheck(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.refs = [], []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a" and attrs.get("href", "").startswith("#"):
            self.refs.append(attrs["href"][1:])


def pack(output: Path, ref: str, registry_ref: str) -> Path:
    sha, tree = git("rev-parse", ref + "^{commit}"), git("rev-parse", ref + "^{tree}")
    registry_sha = git("rev-parse", registry_ref + "^{commit}")
    destination = output.resolve() / f"AutoTrade_Baseline_{sha[:12]}"
    destination.mkdir(parents=True, exist_ok=False)
    archive = subprocess.check_output(["git", "-C", str(ROOT), "archive", sha])
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(destination, filter="data")
    check(destination)
    registry_dir = destination / "registry_snapshot"
    registry_dir.mkdir()
    for name in ("registry.json", "transitions.ndjson", "REGISTRY.md"):
        (registry_dir / name).write_bytes(subprocess.check_output(["git", "-C", str(ROOT), "show", registry_sha + ":" + name]))
    identity = {"repository": "https://github.com/Oleksii-debug/AutoTrade", "commit": sha, "tree": tree, "registry_commit": registry_sha, "registry_snapshot_is_ownership_authority": False, "manifest_excludes": ["MANIFEST_SHA256.json", "outer ZIP", "ZIP SHA sidecar"]}
    (destination / "SOURCE_REVISION.json").write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
    page = reading_edition(destination)
    links = LinkCheck()
    links.feed(page)
    if len(set(links.ids)) != len(links.ids) or not set(links.refs) <= set(links.ids):
        raise ValueError("invalid HTML anchors")
    (destination / "AUTOTRADE_READING_EDITION.html").write_text(page, encoding="utf-8")
    files = sorted(p for p in destination.rglob("*") if p.is_file())
    manifest = {"schema_version": "1.0.0", "source_commit": sha, "files": [{"path": p.relative_to(destination).as_posix(), "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
    (destination / "MANIFEST_SHA256.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    files.append(destination / "MANIFEST_SHA256.json")
    zipped = destination.with_suffix(".zip")
    with zipfile.ZipFile(zipped, "w", zipfile.ZIP_DEFLATED) as target:
        for path in sorted(files):
            entry = zipfile.ZipInfo(destination.name + "/" + path.relative_to(destination).as_posix())
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            target.writestr(entry, path.read_bytes())
    verify_archive(zipped)
    zipped.with_suffix(".zip.sha256").write_text(hashlib.sha256(zipped.read_bytes()).hexdigest() + "  " + zipped.name + "\n", encoding="utf-8")
    return zipped


def verify_archive(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP CRC mismatch")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("duplicate ZIP entry")
        manifests = [n for n in names if n.endswith("/MANIFEST_SHA256.json")]
        if len(manifests) != 1:
            raise ValueError("exactly one manifest required")
        name = manifests[0]
        prefix = name.removesuffix("MANIFEST_SHA256.json")
        manifest = json.loads(archive.read(name))
        if set(names) != {prefix + f["path"] for f in manifest["files"]} | {name}:
            raise ValueError("manifest/ZIP membership mismatch")
        for item in manifest["files"]:
            data = archive.read(prefix + item["path"])
            if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("checksum mismatch: " + item["path"])
        return {"verified_files": len(manifest["files"]), "source_commit": manifest["source_commit"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "refresh", "pack", "verify"])
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--registry-ref", default="origin/control/registry")
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.command == "check":
        print(json.dumps(check()))
    elif args.command == "refresh":
        refresh()
        print(json.dumps(check()))
    elif args.command == "pack":
        print(pack(args.output, args.ref, args.registry_ref))
    elif args.command == "verify":
        if not args.archive:
            parser.error("--archive is required")
        print(json.dumps(verify_archive(args.archive)))
