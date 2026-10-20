"""Durable offline alpha jobs over the canonical research evidence stores.

A completed training checkpoint contains native model artifacts and one shared
specialist DAG receipt. Recovery verifies and replays those bytes without fit.
No job, candidate record or receipt grants financial or promotion authority.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from time import monotonic
from uuid import NAMESPACE_URL, UUID, uuid5

import numpy as np

from autotrade_runtime.artifacts import ArtifactStore, trusted_authenticated_reader
from research.autotrade_research.integrations.alpha_economics import AlphaInputError, CostBudget
from research.autotrade_research.integrations.alpha_models import (
    BACKENDS, PriceSeries, make_split, predict_model_artifact,
)
from research.autotrade_research.integrations.alpha_packet import build_alpha_packet
from research.autotrade_research.jobs import ResearchJobStore, JobConflictError, JobLeaseError
from research.autotrade_research.science.registry import ScientificRegistry

from .alpha_dag_binding import aggregate_alpha_receipt, publish_alpha_research

_RIGHTS = {"storage": True, "export": False}
_LEASE_SECONDS = 900


def _bytes(document: dict) -> bytes:
    try:
        data = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as error:
        raise AlphaInputError("alpha job requires finite JSON") from error
    if len(data) > 1_000_000:
        raise AlphaInputError("alpha job document exceeds the bounded contract")
    return data


def _document(data: bytes) -> dict:
    try:
        document = json.loads(data)
        if type(document) is not dict or _bytes(document) != data:
            raise AlphaInputError("alpha job document must be canonical JSON")
        return document
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise AlphaInputError("invalid alpha job document") from error


def _ref(manifest: dict) -> str:
    return f"artifact:{manifest['artifact_id']}@{manifest['sha256']}"


def _request_id(digest: str) -> str:
    return str(uuid5(NAMESPACE_URL, "autotrade:alpha-request:" + digest))


def _source(payload: dict) -> tuple[PriceSeries, CostBudget]:
    if type(payload) is not dict or set(payload) != {"series", "costs"}:
        raise AlphaInputError("alpha input requires exactly series and costs")
    if type(payload["series"]) is not dict or type(payload["costs"]) is not dict:
        raise AlphaInputError("alpha source and costs must be records")
    values = dict(payload["series"])
    for name in ("closes", "closed_at_ms", "available_at_ms"):
        if type(values.get(name)) is not list:
            raise AlphaInputError("alpha source arrays must be lists")
        values[name] = tuple(values[name])
    try:
        source, costs = PriceSeries(**values), CostBudget(**payload["costs"])
        source.arrays()
        costs.validate(source.asof_ms)
        if source.market.split("_")[-1] != costs.quote_currency:
            raise AlphaInputError("alpha costs require the market quote currency")
        return source, costs
    except (TypeError, KeyError) as error:
        raise AlphaInputError("invalid alpha source or cost fields") from error


def _implementation() -> dict[str, str]:
    from research.autotrade_research.integrations import alpha_models, alpha_model_checkpoint, alpha_packet
    from . import alpha_dag_binding
    modules = (alpha_models, alpha_model_checkpoint, alpha_packet, alpha_dag_binding)
    result = {module.__name__: sha256(Path(module.__file__).read_bytes()).hexdigest()
              for module in modules}
    result["alpha_research_worker"] = sha256(Path(__file__).read_bytes()).hexdigest()
    return result


class AlphaResearchWorker:
    def __init__(self, *, jobs: ResearchJobStore, registry: ScientificRegistry, artifact_root: str | Path):
        if type(jobs) is not ResearchJobStore or type(registry) is not ScientificRegistry:
            raise TypeError("alpha worker requires the canonical research stores")
        self.root = Path(artifact_root).resolve()
        candidate = ArtifactStore(self.root)
        self.artifacts = jobs._require_publication_store(candidate)
        self.reader = trusted_authenticated_reader(self.root, publication_store=self.artifacts)
        self.jobs, self.registry = jobs, registry

    def _read(self, reference: str, kind: str, *, limit: int = 1_000_000) -> tuple[dict, bytes]:
        if type(reference) is not str or not reference.startswith("artifact:"):
            raise AlphaInputError("immutable alpha artifact reference required")
        try:
            identifier, digest = reference[9:].split("@sha256:", 1)
            if (str(UUID(identifier)) != identifier or len(digest) != 64 or
                    any(c not in "0123456789abcdef" for c in digest)):
                raise ValueError("noncanonical reference")
            manifest, data = self.reader(identifier)
            if (type(data) is not bytes or not 1 <= len(data) <= limit or
                    manifest.get("sha256") != "sha256:" + digest or
                    sha256(data).hexdigest() != digest or
                    manifest.get("metadata", {}).get("artifact_kind") != kind):
                raise AlphaInputError("alpha artifact identity or kind differs")
            return manifest, data
        except (ValueError, TypeError, KeyError, OSError) as error:
            raise AlphaInputError("alpha artifact could not be authenticated") from error

    def _publish(self, document: dict, kind: str, sources: list[str]) -> tuple[str, dict]:
        data = _bytes(document)
        identity = kind + ":" + sha256(data).hexdigest() + ":" + json.dumps(sources, separators=(",", ":"))
        manifest = self.artifacts.publish_bytes(
            artifact_id=str(uuid5(NAMESPACE_URL, "autotrade:" + identity)),
            data=data, media_type="application/json", rights=dict(_RIGHTS),
            source_refs=sources, metadata={"artifact_kind": kind})
        return _ref(manifest), manifest

    def enqueue(self, payload: dict, *, lookback: int = 24, horizon: int = 1,
                steps: int = 32, seed: int = 7, backends: tuple[str, ...] = BACKENDS) -> tuple[dict, bool]:
        payload = _document(_bytes(payload))
        source, _costs = _source(payload)
        make_split(source, lookback=lookback, horizon=horizon)
        if (type(steps) is not int or not 1 <= steps <= 128 or
                type(seed) is not int or not 0 <= seed <= 2147483647 or
                type(backends) is not tuple or not 1 <= len(backends) <= 3 or
                any(type(name) is not str or name not in BACKENDS for name in backends) or
                len(set(backends)) != len(backends)):
            raise AlphaInputError("invalid bounded alpha training configuration")
        request = {"schema": "autotrade.alpha-request.v1", "payload": payload,
                   "training": {"lookback": lookback, "horizon": horizon, "steps": steps,
                                "seed": seed, "backends": list(backends)},
                   "implementation": _implementation()}
        data = _bytes(request)
        digest = "sha256:" + sha256(data).hexdigest()
        self.artifacts.publish_bytes(
            artifact_id=_request_id(digest), data=data, media_type="application/json",
            rights=dict(_RIGHTS), source_refs=[], metadata={"artifact_kind": "ALPHA_RESEARCH_REQUEST"})
        return self.jobs.enqueue(
            kind="research.alpha", dedupe_key="alpha:" + digest, input_hashes=[digest],
            resource_budget={"wall_seconds": _LEASE_SECONDS}, lease_requeueable=True)

    def _request(self, job: dict) -> tuple[str, dict]:
        if (job["kind"] != "research.alpha" or len(job["input_hashes"]) != 1 or
                job["resource_budget"] != {"wall_seconds": float(_LEASE_SECONDS)}):
            raise AlphaInputError("job does not carry the bounded alpha contract")
        digest = job["input_hashes"][0]
        reference = f"artifact:{_request_id(digest)}@{digest}"
        manifest, data = self._read(reference, "ALPHA_RESEARCH_REQUEST")
        document = _document(data)
        if (set(document) != {"schema", "payload", "training", "implementation"} or
                document["schema"] != "autotrade.alpha-request.v1" or manifest["source_refs"] != [] or
                document["implementation"] != _implementation()):
            raise AlphaInputError("alpha request implementation differs; enqueue a new job")
        return reference, document

    def _verify_candidate(self, candidate: dict, request_ref: str, request: dict) -> dict:
        if (type(candidate) is not dict or
                set(candidate) != {"schema", "request_ref", "model_refs", "dag", "evidence_class",
                                   "live_authority", "automatic_promotion"} or
                candidate["schema"] != "autotrade.alpha-candidate.v1" or
                candidate["request_ref"] != request_ref or candidate["live_authority"] is not False or
                candidate["automatic_promotion"] is not False):
            raise AlphaInputError("alpha checkpoint candidate identity differs")
        source, _costs = _source(request["payload"])
        config = request["training"]
        split = make_split(source, lookback=config["lookback"], horizon=config["horizon"])
        refs = candidate["model_refs"]
        if (type(refs) is not dict or set(refs) != set(config["backends"]) or
                candidate["evidence_class"] != source.evidence_class):
            raise AlphaInputError("alpha checkpoint model set or evidence class differs")
        dag = candidate["dag"]
        if type(dag) is not dict or "packet_ref" not in dag:
            raise AlphaInputError("alpha checkpoint DAG receipt is absent")
        replay = aggregate_alpha_receipt(
            self.root, dag["packet_ref"], expected_snapshot_id=source.snapshot_id, asof_ms=source.asof_ms)
        if replay != dag:
            raise AlphaInputError("alpha checkpoint DAG receipt differs")
        _manifest, packet_bytes = self._read(dag["packet_ref"], "ALPHA_RESEARCH_PACKET")
        packet = _document(packet_bytes)["packet"]
        _input_manifest, input_bytes = self._read(dag["input_ref"], "ALPHA_RESEARCH_INPUT")
        if _document(input_bytes) != request["payload"]:
            raise AlphaInputError("alpha DAG input differs from the frozen request")
        features = np.vstack((split["x"], split["current"]))
        training = {key: value for key, value in config.items() if key != "backends"}
        for backend in config["backends"]:
            manifest, data = self._read(refs[backend], "ALPHA_NATIVE_MODEL", limit=4_000_000)
            envelope = json.loads(data)
            report = packet["forecast"]["models"][backend]
            if (manifest["source_refs"] != [request_ref] or envelope.get("backend") != backend or
                    envelope.get("series") != request["payload"]["series"] or
                    envelope.get("training") != training or
                    report.get("model_artifact_sha256") != manifest["sha256"][7:]):
                raise AlphaInputError("native model does not belong to this alpha request and packet")
            predictions = predict_model_artifact(data, expected_sha256=manifest["sha256"][7:], features=features)
            held = split["holdout"]
            held_error = float(np.mean(np.abs(split["y"][held] - predictions[held])))
            if (not np.isclose(predictions[-1], float(report["gross_midpoint_return_ratio"]), rtol=0, atol=1e-10) or
                    not np.isclose(held_error, report["holdout_mae"], rtol=0, atol=1e-10)):
                raise AlphaInputError("native replay differs from the frozen forecast")
        return candidate

    def run(self, job_id: str, *, worker_id: str = "alpha-cpu") -> dict:
        job = self.jobs.get(job_id)
        request_ref, request = self._request(job)
        if job["state"] == "SUCCEEDED":
            if len(job["output_refs"]) != 1:
                raise AlphaInputError("alpha job requires one accepted result")
            manifest, data = self._read(job["output_refs"][0], "RESEARCH_JOB_RESULT")
            metadata = manifest["metadata"]
            if (manifest["source_refs"] != job["input_hashes"] or metadata.get("job_id") != job["job_id"] or
                    type(metadata.get("job_generation")) is not int or metadata.get("job_generation") != int(job["generation"])):
                raise AlphaInputError("alpha result does not belong to the accepted job generation")
            result = _document(data)
            if (set(result) != {"schema", "candidate_ref", "candidate", "live_authority", "automatic_promotion"} or
                    result["schema"] != "autotrade.alpha-job-result.v1" or result["live_authority"] is not False or
                    result["automatic_promotion"] is not False):
                raise AlphaInputError("invalid alpha job result")
            _manifest, candidate_bytes = self._read(result["candidate_ref"], "ALPHA_MODEL_CANDIDATE")
            if candidate_bytes != _bytes(result["candidate"]):
                raise AlphaInputError("alpha result candidate reference differs")
            self._verify_candidate(result["candidate"], request_ref, request)
            return result

        self.jobs.requeue_expired()
        job = self.jobs.claim(worker_id, lease_seconds=_LEASE_SECONDS, job_id=job_id)
        if job is None:
            raise JobLeaseError("alpha job is paused, owned, cancelled or not runnable")
        generation = int(job["generation"])
        started = monotonic()
        checkpoint = self.jobs.read_checkpoint_bytes(job_id, worker_id=worker_id, generation=generation)
        if checkpoint is None:
            source, costs = _source(request["payload"])
            config = dict(request["training"])
            config["backends"] = tuple(config["backends"])
            artifacts: dict[str, bytes] = {}
            packet = build_alpha_packet(source, costs, **config, model_artifacts=artifacts)
            model_refs = {}
            for backend in config["backends"]:
                data = artifacts[backend]
                digest = sha256(data).hexdigest()
                manifest = self.artifacts.publish_bytes(
                    artifact_id=str(uuid5(NAMESPACE_URL, "autotrade:alpha-model:" + request_ref + ":" + digest)),
                    data=data, media_type="application/json", rights=dict(_RIGHTS),
                    source_refs=[request_ref], metadata={"artifact_kind": "ALPHA_NATIVE_MODEL", "backend": backend})
                model_refs[backend] = _ref(manifest)
            candidate = {"schema": "autotrade.alpha-candidate.v1", "request_ref": request_ref,
                         "model_refs": model_refs,
                         "dag": publish_alpha_research(self.root, request["payload"], packet),
                         "evidence_class": source.evidence_class,
                         "live_authority": False, "automatic_promotion": False}
            self.jobs.publish_checkpoint_bytes(
                job_id, worker_id=worker_id, generation=generation, artifact_store=self.artifacts,
                data=_bytes(candidate), media_type="application/json", rights=dict(_RIGHTS),
                resource_usage={"wall_seconds": job["resource_usage"].get("wall_seconds", 0) + monotonic() - started})
        else:
            candidate = _document(checkpoint[1])
        self._verify_candidate(candidate, request_ref, request)
        sources = ([request_ref] + [candidate["model_refs"][name] for name in request["training"]["backends"]] +
                   [candidate["dag"]["packet_ref"], candidate["dag"]["result_ref"]])
        candidate_ref, _manifest = self._publish(candidate, "ALPHA_MODEL_CANDIDATE", sources)
        self.registry.register_research_model_candidate(candidate_ref, authoritative_artifact_root=self.root)
        result = {"schema": "autotrade.alpha-job-result.v1", "candidate_ref": candidate_ref,
                  "candidate": candidate, "live_authority": False, "automatic_promotion": False}
        _manifest, accepted = self.jobs.publish_result_bytes(
            job_id, worker_id=worker_id, generation=generation, artifact_store=self.artifacts,
            data=_bytes(result), media_type="application/json", rights=dict(_RIGHTS))
        if not accepted:
            raise JobConflictError("alpha result was not accepted by the current job generation")
        return result
