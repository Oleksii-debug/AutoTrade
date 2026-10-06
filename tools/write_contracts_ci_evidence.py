"""Write exact WP-01 contract evidence after contract verification succeeds."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tomllib

from tools.write_ci_evidence import build_evidence


ROOT = Path(__file__).resolve().parents[1]

BASE_VERIFIED_COMMANDS = (
    "python tools/generate_common_scalar_bindings.py --check",
    "python tools/generate_host_api_routes.py --check",
    "python tools/generate_common_scalar_corpus.py --check",
    "python -m unittest discover -s tests/Contracts -v",
    "python -m pip install . --no-deps --force-reinstall",
    (
        "python -c \"import os,tempfile,importlib.metadata as m; "
        "os.chdir(tempfile.gettempdir()); "
        "import autotrade_numeric.dataset_manifest as d; "
        "assert m.version('autotrade-exact-numeric') == '0.0.2'; "
        "assert d.CONTRACT_VERSION == '6.0.0'; "
        "assert d.DATASET_MANIFEST_SEMANTIC_VALIDATOR_ID == "
        "'dataset-manifest-content-authority-v1'\""
    ),
    (
        "dotnet run --project tests/Contracts.DotNet/Contracts.DotNet.csproj "
        "--configuration Release -- "
        "contracts/fixtures/common-scalars.corpus.json "
        "contracts/fixtures/dataset-manifest.semantic.corpus.json"
    ),
    "node tests/Contracts.TypeScript/common-scalars.test.cjs",
    "node tests/Contracts.TypeScript/dataset-manifest.test.cjs",
    "node tests/Contracts.TypeScript/host-api-routes.test.cjs",
)

UNRESOLVED_LIMITS = (
    "Contract verification does not establish provider qualification or provider-origin truth.",
    "Contract verification does not authorize PAPER or LIVE trading.",
    "Contract verification does not establish release or Windows/NVDA qualification.",
    "Contract verification does not prove profitability or economic edge.",
)


def _json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.relative_to(ROOT)} must contain a JSON object")
    return payload


def _project(path: Path) -> dict:
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    project = payload.get("project")
    if not isinstance(project, dict):
        raise ValueError(f"{path.relative_to(ROOT)} must define [project]")
    return project


def _input_versions() -> dict[str, object]:
    manifest = _json(ROOT / "contracts" / "manifest.json")
    fixtures = _json(ROOT / "contracts" / "fixtures" / "manifest.json")
    package = _project(ROOT / "pyproject.toml")
    research = _project(ROOT / "research" / "pyproject.toml")

    contract_version = manifest.get("contract_version")
    openapi = manifest.get("openapi")
    if not isinstance(openapi, dict):
        raise ValueError("contracts manifest must define openapi")
    openapi_version = openapi.get("version")
    corpus_version = fixtures.get("corpus_version")
    if (
        not isinstance(contract_version, str)
        or openapi_version != contract_version
        or corpus_version != contract_version
    ):
        raise ValueError(
            "contract, OpenAPI and fixture corpus versions must be identical"
        )

    package_version = package.get("version")
    if not isinstance(package_version, str) or not package_version:
        raise ValueError("autotrade-exact-numeric package version is required")
    dependencies = research.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise ValueError("research dependencies must be an array")
    expected_dependency = f"autotrade-exact-numeric=={package_version}"
    if expected_dependency not in dependencies:
        raise ValueError(
            "research must depend exactly on the current exact-numeric package"
        )

    validators = manifest.get("semantic_validators", [])
    if not isinstance(validators, list) or not validators:
        raise ValueError("at least one semantic validator must be declared")
    semantic_validators: list[dict[str, str]] = []
    for entry in validators:
        if not isinstance(entry, dict):
            raise ValueError("semantic validator declaration must be an object")
        values = {
            key: entry.get(key)
            for key in ("id", "schema", "definition", "corpus")
        }
        if not all(isinstance(value, str) and value for value in values.values()):
            raise ValueError("semantic validator identity fields must be non-empty text")
        corpus_path = ROOT / values["corpus"]
        if not corpus_path.is_file():
            raise ValueError(
                f"semantic validator corpus does not exist: {values['corpus']}"
            )
        semantic_validators.append(values)

    return {
        "contract_version": contract_version,
        "json_schema_draft": manifest.get("json_schema_draft"),
        "schema_base_uri": manifest.get("schema_base_uri"),
        "schemas": list(manifest.get("schemas", [])),
        "openapi_version": openapi_version,
        "fixture_corpus_version": corpus_version,
        "exact_numeric_package_version": package_version,
        "research_exact_numeric_dependency": expected_dependency,
        "semantic_validators": semantic_validators,
    }


def verified_commands(event_name: str) -> list[str]:
    commands = list(BASE_VERIFIED_COMMANDS)
    if event_name == "pull_request":
        base_ref = os.environ.get("GITHUB_BASE_REF", "").strip()
        if not base_ref:
            raise ValueError("GITHUB_BASE_REF is required for pull_request evidence")
        commands.append(
            f"python tools/contract_version_guard.py --base-ref origin/{base_ref}"
        )
    return commands


def build_contracts_evidence() -> dict[str, object]:
    evidence = build_evidence(
        suite="contracts",
        command="WP-01 canonical contract qualification",
    )
    event_name = str(evidence["github"]["event_name"])
    evidence["input_versions"] = _input_versions()
    evidence["tested_commands"] = verified_commands(event_name)
    evidence["unresolved_limits"] = list(UNRESOLVED_LIMITS)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evidence = build_contracts_evidence()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
