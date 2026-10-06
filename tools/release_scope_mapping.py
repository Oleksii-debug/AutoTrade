from __future__ import annotations

import base64
import binascii
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re

SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SPDX_ID = re.compile(r"^SPDXRef-[A-Za-z0-9.-]+$")
_ALLOWED_SCOPE = frozenset({
    "DISTRIBUTED_RUNTIME",
    "IMPORTED_FIRST_PARTY_SOURCE",
    "SEMANTIC_REFERENCE_NOT_DISTRIBUTED",
    "INSPECTED_CANDIDATE_NOT_DISTRIBUTED",
})


class ReleaseScopeMappingError(ValueError):
    pass


def _reject_constant(value: str) -> None:
    raise ReleaseScopeMappingError(f"non-standard JSON constant: {value}")


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ReleaseScopeMappingError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def strict_json_bytes(raw: bytes, *, label: str):
    if type(raw) is not bytes:
        raise TypeError(f"{label} must be exact bytes")
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseScopeMappingError(f"{label} is not strict UTF-8 JSON") from error


def _text(value, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ReleaseScopeMappingError(f"{name} must be canonical non-empty text")
    return value


def _digest(value, *, name: str) -> str:
    value = _text(value, name=name)
    if SHA256_ID.fullmatch(value) is None:
        raise ReleaseScopeMappingError(f"{name} must be canonical sha256")
    return value


def _git_sha(value, *, name: str) -> str:
    value = _text(value, name=name)
    if GIT_SHA.fullmatch(value) is None:
        raise ReleaseScopeMappingError(f"{name} must be canonical Git SHA")
    return value


def _path(value, *, name: str) -> str:
    value = _text(value, name=name)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ReleaseScopeMappingError(
            f"{name} must be canonical relative POSIX path"
        )
    return value


def canonical_json_bytes(value) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def normalize_composition(document) -> dict[str, object]:
    expected = {
        "schema_version", "product", "source_sha",
        "dependency_lock_sha256", "sbom_sha256",
        "schema_compatibility", "runtime", "components",
    }
    if type(document) is not dict or set(document) != expected:
        raise ReleaseScopeMappingError("composition fields mismatch")
    if document["schema_version"] != "1.0.0" or document["product"] != "AutoTrade":
        raise ReleaseScopeMappingError("unsupported composition identity")
    source_sha = _git_sha(document["source_sha"], name="composition source_sha")
    dependency_lock = _digest(
        document["dependency_lock_sha256"], name="dependency_lock_sha256"
    )
    sbom_digest = _digest(document["sbom_sha256"], name="sbom_sha256")
    schema = document["schema_compatibility"]
    runtime = document["runtime"]
    if type(schema) is not dict or set(schema) != {"minimum", "maximum"}:
        raise ReleaseScopeMappingError("schema compatibility fields mismatch")
    if type(runtime) is not dict or set(runtime) != {
        "architecture", "runtime_identifier", "minimum_windows_version"
    }:
        raise ReleaseScopeMappingError("runtime fields mismatch")
    normalized_schema = {
        key: _text(schema[key], name=f"schema_compatibility.{key}")
        for key in ("minimum", "maximum")
    }
    normalized_runtime = {
        key: _text(runtime[key], name=f"runtime.{key}")
        for key in ("architecture", "runtime_identifier", "minimum_windows_version")
    }
    values = document["components"]
    if type(values) is not list or not values:
        raise ReleaseScopeMappingError("composition components must be non-empty list")
    seen_ids = set()
    seen_paths = set()
    components = []
    for index, item in enumerate(values):
        if type(item) is not dict or set(item) != {
            "component_id", "kind", "path", "version", "sha256"
        }:
            raise ReleaseScopeMappingError(
                f"composition component[{index}] fields mismatch"
            )
        record = {
            "component_id": _text(
                item["component_id"], name=f"component[{index}].component_id"
            ),
            "kind": _text(item["kind"], name=f"component[{index}].kind"),
            "path": _path(item["path"], name=f"component[{index}].path"),
            "version": _text(item["version"], name=f"component[{index}].version"),
            "sha256": _digest(item["sha256"], name=f"component[{index}].sha256"),
        }
        if record["component_id"] in seen_ids or record["path"] in seen_paths:
            raise ReleaseScopeMappingError(
                "composition component identity/path duplicated"
            )
        seen_ids.add(record["component_id"])
        seen_paths.add(record["path"])
        components.append(record)
    for kind, digest in (
        ("dependency-lock", dependency_lock),
        ("sbom", sbom_digest),
    ):
        matches = [item for item in components if item["kind"] == kind]
        if len(matches) != 1 or matches[0]["sha256"] != digest:
            raise ReleaseScopeMappingError(
                f"composition requires exactly one matching {kind}"
            )
    return {
        "schema_version": "1.0.0",
        "product": "AutoTrade",
        "source_sha": source_sha,
        "dependency_lock_sha256": dependency_lock,
        "sbom_sha256": sbom_digest,
        "schema_compatibility": normalized_schema,
        "runtime": normalized_runtime,
        "components": sorted(components, key=lambda item: item["path"]),
    }


def _sha512_hex(value) -> str:
    value = _text(value, name="NuGet content hash")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ReleaseScopeMappingError(
            "NuGet content hash is invalid base64"
        ) from error
    if len(raw) != 64:
        raise ReleaseScopeMappingError("NuGet content hash is not SHA-512")
    return raw.hex()


def _nuget_purl(name: str, version: str) -> str:
    return f"pkg:nuget/{name}@{version}"


def normalize_spdx_packages(sbom) -> dict[str, dict[str, object]]:
    if (
        type(sbom) is not dict
        or sbom.get("spdxVersion") != "SPDX-2.3"
        or sbom.get("SPDXID") != "SPDXRef-DOCUMENT"
    ):
        raise ReleaseScopeMappingError("SBOM must be SPDX-2.3 document")
    packages = sbom.get("packages")
    if type(packages) is not list:
        raise ReleaseScopeMappingError("SBOM packages must be list")
    by_purl = {}
    for index, package in enumerate(packages):
        if type(package) is not dict:
            raise ReleaseScopeMappingError(f"SBOM package[{index}] must be object")
        spdx_id = _text(
            package.get("SPDXID"), name=f"SBOM package[{index}].SPDXID"
        )
        if SPDX_ID.fullmatch(spdx_id) is None:
            raise ReleaseScopeMappingError("SBOM package SPDXID invalid")
        name = _text(package.get("name"), name=f"SBOM package[{index}].name")
        version = _text(
            package.get("versionInfo"), name=f"SBOM package[{index}].versionInfo"
        )
        refs = package.get("externalRefs", [])
        if type(refs) is not list:
            raise ReleaseScopeMappingError("SBOM externalRefs must be list")
        purls = []
        for ref in refs:
            if type(ref) is not dict:
                raise ReleaseScopeMappingError("SBOM externalRef must be object")
            if ref.get("referenceType") == "purl":
                purls.append(
                    _text(ref.get("referenceLocator"), name="SBOM purl")
                )
        if not purls:
            continue
        if len(purls) != 1 or purls[0] in by_purl:
            raise ReleaseScopeMappingError("SBOM purl identity ambiguous")
        checksums = package.get("checksums", [])
        if type(checksums) is not list:
            raise ReleaseScopeMappingError("SBOM checksums must be list")
        sha512_values = []
        for checksum in checksums:
            if type(checksum) is not dict:
                raise ReleaseScopeMappingError("SBOM checksum must be object")
            if checksum.get("algorithm") == "SHA512":
                sha512_values.append(
                    _text(
                        checksum.get("checksumValue"),
                        name="SBOM SHA512",
                    ).lower()
                )
        if (
            len(sha512_values) != 1
            or re.fullmatch(r"[0-9a-f]{128}", sha512_values[0]) is None
        ):
            raise ReleaseScopeMappingError(
                "SBOM package requires one canonical SHA512 checksum"
            )
        by_purl[purls[0]] = {
            "spdx_id": spdx_id,
            "name": name,
            "version": version,
            "sha512": sha512_values[0],
        }
    return by_purl


def build_mapping(
    *,
    composition,
    sbom,
    sbom_raw: bytes,
    locked_packages,
    package_rights,
    provenance_components,
) -> dict[str, object]:
    composition = normalize_composition(composition)
    if "sha256:" + sha256(sbom_raw).hexdigest() != composition["sbom_sha256"]:
        raise ReleaseScopeMappingError("SBOM bytes do not match composition")
    sbom_packages = normalize_spdx_packages(sbom)

    rights = {}
    for record in package_rights:
        if type(record) is not dict:
            raise ReleaseScopeMappingError("package rights record must be object")
        key = (
            _text(record.get("name"), name="rights name"),
            _text(record.get("version"), name="rights version"),
            _text(
                record.get("content_hash_sha512_base64"),
                name="rights hash",
            ),
        )
        if key in rights:
            raise ReleaseScopeMappingError("duplicate package-rights identity")
        rights[key] = record

    packages = []
    seen = set()
    for item in locked_packages:
        if type(item) is not dict:
            raise ReleaseScopeMappingError("locked package record must be object")
        name = _text(item.get("name"), name="locked package name")
        version = _text(item.get("version"), name="locked package version")
        content_hash = _text(
            item.get("content_hash_sha512_base64"),
            name="locked package hash",
        )
        key = (name, version, content_hash)
        if key in seen:
            continue
        seen.add(key)
        if key not in rights:
            raise ReleaseScopeMappingError(
                f"locked package lacks exact rights record: {name}@{version}"
            )
        purl = _nuget_purl(name, version)
        package = sbom_packages.get(purl)
        if package is None:
            raise ReleaseScopeMappingError(
                f"locked package missing from SBOM: {name}@{version}"
            )
        if package["name"] != name or package["version"] != version:
            raise ReleaseScopeMappingError(
                f"SBOM package identity mismatch: {name}@{version}"
            )
        if package["sha512"] != _sha512_hex(content_hash):
            raise ReleaseScopeMappingError(
                f"SBOM package hash mismatch: {name}@{version}"
            )
        packages.append({
            "ecosystem": "nuget",
            "name": name,
            "version": version,
            "purl": purl,
            "content_hash_sha512_base64": content_hash,
            "sbom_spdx_id": package["spdx_id"],
        })
    locked_purls = {item["purl"] for item in packages}
    extras = sorted(set(sbom_packages) - locked_purls)
    if extras:
        raise ReleaseScopeMappingError(
            "SBOM contains unmapped external package(s): " + ", ".join(extras)
        )

    scope = []
    unresolved_rights = []
    for item in provenance_components:
        if type(item) is not dict:
            raise ReleaseScopeMappingError("provenance component must be object")
        name = _text(item.get("name"), name="provenance component name")
        classification = _text(
            item.get("release_scope_classification"),
            name=f"{name} release_scope_classification",
        )
        if classification not in _ALLOWED_SCOPE:
            raise ReleaseScopeMappingError(
                f"unsupported release scope classification: {name}"
            )
        state = _text(
            item.get("release_distribution_state"),
            name=f"{name} release_distribution_state",
        )
        record = {
            "name": name,
            "repository": _text(
                item.get("repository"), name=f"{name} repository"
            ),
            "revision": _text(
                item.get("revision"), name=f"{name} revision"
            ),
            "release_scope_classification": classification,
            "release_distribution_state": state,
        }
        scope.append(record)
        if (
            classification in {
                "DISTRIBUTED_RUNTIME",
                "IMPORTED_FIRST_PARTY_SOURCE",
            }
            and state != "APPROVED"
        ):
            unresolved_rights.append(name)
    scope.sort(key=lambda item: item["name"].casefold())

    result = {
        "schema_version": "1.0.0",
        "source_sha": composition["source_sha"],
        "composition_sha256": (
            "sha256:" + sha256(canonical_json_bytes(composition)).hexdigest()
        ),
        "dependency_lock_sha256": composition["dependency_lock_sha256"],
        "sbom_sha256": composition["sbom_sha256"],
        "distributed_packages": sorted(
            packages,
            key=lambda item: (
                item["ecosystem"], item["name"].casefold(), item["version"]
            ),
        ),
        "provenance_scope": scope,
        "unresolved_distribution_rights": sorted(set(unresolved_rights)),
    }
    return {
        **result,
        "mapping_digest": (
            "sha256:" + sha256(canonical_json_bytes(result)).hexdigest()
        ),
    }


def build_repository_mapping(*, root: Path, composition_path: Path, sbom_path: Path):
    try:
        from tools.dotnet_package_rights import (
            locked_package_artifacts,
            package_rights_records,
        )
    except ImportError:
        from dotnet_package_rights import (
            locked_package_artifacts,
            package_rights_records,
        )
    root = root.resolve()
    composition_path = composition_path.resolve()
    sbom_path = sbom_path.resolve()
    for path, label in (
        (composition_path, "composition"),
        (sbom_path, "SBOM"),
    ):
        if not path.is_file() or not path.is_relative_to(root):
            raise ReleaseScopeMappingError(
                f"{label} path must be an existing repository file"
            )
    composition = strict_json_bytes(
        composition_path.read_bytes(), label="release composition"
    )
    sbom_raw = sbom_path.read_bytes()
    sbom = strict_json_bytes(sbom_raw, label="release SBOM")
    components = strict_json_bytes(
        (root / "provenance" / "components.json").read_bytes(),
        label="components provenance",
    )
    if (
        type(components) is not dict
        or components.get("schema_version") != "1.0.0"
        or type(components.get("components")) is not list
    ):
        raise ReleaseScopeMappingError(
            "components provenance schema is unsupported"
        )
    return build_mapping(
        composition=composition,
        sbom=sbom,
        sbom_raw=sbom_raw,
        locked_packages=locked_package_artifacts(root),
        package_rights=package_rights_records(root),
        provenance_components=components["components"],
    )


def rendered_mapping(mapping) -> str:
    return canonical_json_bytes(mapping).decode("utf-8")


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--composition", required=True)
    parser.add_argument("--sbom", required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        mapping = build_repository_mapping(
            root=root,
            composition_path=root / args.composition,
            sbom_path=root / args.sbom,
        )
    except (OSError, ReleaseScopeMappingError, ValueError) as error:
        print(str(error))
        return 1
    print(rendered_mapping(mapping), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
