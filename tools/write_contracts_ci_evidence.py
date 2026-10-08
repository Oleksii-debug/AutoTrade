"""Write exact WP-01 contract evidence after contract verification succeeds."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tomllib

try:
    from tools.write_ci_evidence import build_evidence
except ModuleNotFoundError:  # direct execution: python tools/<script>.py
    from write_ci_evidence import build_evidence


ROOT = Path(__file__).resolve().parents[1]

BASE_VERIFIED_COMMANDS = (
    "python tools/generate_common_scalar_bindings.py --check",
    "python tools/generate_contract_shape_bindings.py --check",
    "python tools/generate_host_api_routes.py --check",
    "python tools/generate_common_scalar_corpus.py --check",
    "python -m unittest discover -s tests/Contracts -v",
    "python -m pip install . --no-deps --force-reinstall",
    (
        "python -c \"import os,tempfile,importlib.metadata as m; "
        "os.chdir(tempfile.gettempdir()); "
        "import autotrade_numeric.dataset_manifest as d; "
        "assert m.version('autotrade-exact-numeric') == '0.0.2'; "
        "assert d.CONTRACT_VERSION == '7.0.0'; "
        "assert d.DATASET_MANIFEST_SEMANTIC_VALIDATOR_ID == "
        "'dataset-manifest-content-authority-v1'\""
    ),
    (
        "dotnet run --project tests/Contracts.DotNet/Contracts.DotNet.csproj "
        "--configuration Release -- "
        "contracts/fixtures/common-scalars.corpus.json "
        "contracts/fixtures/dataset-manifest.semantic.corpus.json "
        "contracts/fixtures/contract-shapes.corpus.json"
    ),
    "node tests/Contracts.TypeScript/common-scalars.test.cjs",
    "node tests/Contracts.TypeScript/dataset-manifest.test.cjs",
    "node tests/Contracts.TypeScript/contract-shapes.test.cjs",
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



def _repo_file(relative: object, *, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label} must be non-empty text")
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError(f"{label} escapes repository root: {relative}")
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {relative}")
    return path

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

    shape_conformance = manifest.get("shape_conformance")
    if not isinstance(shape_conformance, dict):
        raise ValueError("contracts manifest must define shape_conformance")
    if shape_conformance.get("scope") != "closed-object-shape-subset":
        raise ValueError("shape_conformance scope must be closed-object-shape-subset")
    shape_corpus_relative = shape_conformance.get("corpus")
    shape_bindings = shape_conformance.get("bindings")
    if not isinstance(shape_bindings, dict) or set(shape_bindings) != {
        "python",
        "csharp",
        "typescript",
    }:
        raise ValueError(
            "shape_conformance must declare python/csharp/typescript bindings"
        )
    shape_corpus_path = _repo_file(
        shape_corpus_relative,
        label="shape conformance corpus",
    )
    shape_corpus = _json(shape_corpus_path)
    if shape_corpus.get("contract_version") != contract_version:
        raise ValueError("shape conformance corpus contract_version mismatch")
    if shape_corpus.get("scope") != shape_conformance.get("scope"):
        raise ValueError("shape conformance corpus scope mismatch")
    shape_cases = shape_corpus.get("cases")
    if not isinstance(shape_cases, list) or not shape_cases:
        raise ValueError("shape conformance corpus must contain cases")
    checked_shape_bindings: dict[str, str] = {}
    for language, relative in shape_bindings.items():
        _repo_file(relative, label=f"shape conformance {language} binding")
        assert isinstance(relative, str)
        checked_shape_bindings[language] = relative

    schema_names = manifest.get("schemas", [])
    if not isinstance(schema_names, list):
        raise ValueError("contracts manifest schemas must be an array")

    semantic_validators: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    expected_languages = {"python", "csharp", "typescript"}
    for entry in validators:
        if not isinstance(entry, dict):
            raise ValueError("semantic validator declaration must be an object")

        values = {
            key: entry.get(key)
            for key in ("id", "schema", "definition", "corpus")
        }
        if not all(isinstance(value, str) and value for value in values.values()):
            raise ValueError("semantic validator identity fields must be non-empty text")

        validator_id = values["id"]
        schema_name = values["schema"]
        definition = values["definition"]
        corpus_relative = values["corpus"]
        assert isinstance(validator_id, str)
        assert isinstance(schema_name, str)
        assert isinstance(definition, str)
        assert isinstance(corpus_relative, str)

        if validator_id in seen_ids:
            raise ValueError(f"duplicate semantic validator id: {validator_id}")
        seen_ids.add(validator_id)
        if schema_name not in schema_names:
            raise ValueError(
                f"semantic validator {validator_id} schema is not declared in manifest"
            )

        schema_path = _repo_file(
            f"contracts/jsonschema/{schema_name}",
            label=f"semantic validator {validator_id} schema",
        )
        schema_payload = _json(schema_path)
        definitions = schema_payload.get("$defs", {})
        if not isinstance(definitions, dict) or definition not in definitions:
            raise ValueError(
                f"semantic validator {validator_id} definition does not exist"
            )
        definition_payload = definitions[definition]
        if (
            not isinstance(definition_payload, dict)
            or definition_payload.get("x-autotrade-semantic-validator") != validator_id
        ):
            raise ValueError(
                f"semantic validator {validator_id} schema annotation mismatch"
            )

        corpus_path = _repo_file(
            corpus_relative,
            label=f"semantic validator {validator_id} corpus",
        )
        corpus_payload = _json(corpus_path)
        if corpus_payload.get("validator_id") != validator_id:
            raise ValueError(
                f"semantic validator {validator_id} corpus validator_id mismatch"
            )
        if corpus_payload.get("contract_version") != contract_version:
            raise ValueError(
                f"semantic validator {validator_id} corpus contract_version mismatch"
            )
        cases = corpus_payload.get("cases")
        if not isinstance(cases, list) or not cases:
            raise ValueError(
                f"semantic validator {validator_id} corpus must contain cases"
            )

        bindings = entry.get("bindings")
        if not isinstance(bindings, dict) or set(bindings) != expected_languages:
            raise ValueError(
                f"semantic validator {validator_id} must declare python/csharp/typescript bindings"
            )
        checked_bindings: dict[str, str] = {}
        for language, relative in bindings.items():
            _repo_file(
                relative,
                label=f"semantic validator {validator_id} {language} binding",
            )
            assert isinstance(relative, str)
            checked_bindings[language] = relative

        installed = entry.get("installed_bindings", {})
        if not isinstance(installed, dict):
            raise ValueError(
                f"semantic validator {validator_id} installed_bindings must be an object"
            )
        checked_installed: dict[str, str] = {}
        for language, relative in installed.items():
            _repo_file(
                relative,
                label=f"semantic validator {validator_id} installed {language} binding",
            )
            assert isinstance(relative, str)
            checked_installed[language] = relative

        semantic_validators.append(
            {
                **values,
                "bindings": checked_bindings,
                "installed_bindings": checked_installed,
                "case_count": len(cases),
            }
        )

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
        "shape_conformance": {
            "scope": shape_conformance["scope"],
            "corpus": shape_corpus_relative,
            "bindings": checked_shape_bindings,
            "definition_count": shape_corpus.get("definition_count"),
            "case_count": len(shape_cases),
        },
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
