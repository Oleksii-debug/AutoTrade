"""Explicit offline alpha runner -> canonical artifacts -> existing specialist DAG.

The product caller selects the artifact root and expected research cutoff. A
source label is never provider authentication. Only immutable input/output
integrity is established here; results remain advisory and cannot trade, promote
a model, or reinterpret an old forecast as a new live decision.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from autotrade_runtime.artifacts import ArtifactStore, trusted_authenticated_reader
from autotrade_runtime.strict_json import strict_json_loads
from research.autotrade_research.agents.dag import (
    SpecialistRun, SpecialistSpec, aggregate_specialists, plan_specialists,
)
from research.autotrade_research.integrations.alpha_economics import (
    CostBudget, exact, market_name, opportunity, timestamp,
)

MAX_DOCUMENT_BYTES = 1_000_000
_BACKENDS = frozenset({"DLINEAR", "XGBOOST", "CATBOOST"})
_SERIES_FIELDS = {"market", "closes", "closed_at_ms", "available_at_ms",
                  "interval_ms", "asof_ms", "source_ref", "snapshot_id", "evidence_class"}


def _canonical(document: dict) -> bytes:
    if type(document) is not dict:
        raise ValueError("alpha document must be an exact dictionary")
    data = json.dumps(document, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("alpha document exceeds the bounded input contract")
    strict_json_loads(data.decode("utf-8"))
    return data


def _record(document: object, name: str) -> dict:
    if type(document) is not dict:
        raise ValueError(f"{name} must be an exact record")
    return document


def _text(value: object, name: str) -> str:
    if (type(value) is not str or not 1 <= len(value) <= 512 or
            not value.strip() or value != value.strip()):
        raise ValueError(f"bounded {name} is required")
    return value


def _validate(payload: dict, packet: dict, *, snapshot_id: str, asof_ms: int):
    """Readmit the documented component contract without loading model libraries."""
    _text(snapshot_id, "snapshot_id")
    timestamp(asof_ms, "asof_ms")
    if set(payload) != {"series", "costs"}:
        raise ValueError("only research series and cost assumptions are admitted")
    series = _record(payload["series"], "series")
    if set(series) != _SERIES_FIELDS:
        raise ValueError("unexpected series fields")
    market_name(series["market"])
    _text(series["source_ref"], "source_ref")
    if series["snapshot_id"] != snapshot_id or series["asof_ms"] != asof_ms:
        raise ValueError("alpha input snapshot or research cutoff differs")
    timestamp(series["asof_ms"], "series asof_ms")
    if series["evidence_class"] not in ("SYNTHETIC_FIXTURE", "RECORDED_PUBLIC"):
        raise ValueError("unsupported research evidence class")
    prices, closes, available = (series[key] for key in
                                 ("closes", "closed_at_ms", "available_at_ms"))
    if (any(type(value) is not list for value in (prices, closes, available)) or
            not 160 <= len(prices) <= 4096 or len(closes) != len(prices) or
            len(available) != len(prices)):
        raise ValueError("bounded aligned closed bars are required")
    interval = series["interval_ms"]
    if type(interval) is not int or not 1 <= interval <= 86_400_000:
        raise ValueError("invalid bar interval")
    for index, (price, closed, admitted) in enumerate(zip(prices, closes, available)):
        exact(price, "close", low="0.000000000001", high="1000000000000")
        timestamp(closed, "bar close")
        timestamp(admitted, "bar availability")
        if not closed <= admitted <= asof_ms:
            raise ValueError("future or unclosed research input")
        if index and (closed - closes[index - 1] != interval or
                      admitted < available[index - 1]):
            raise ValueError("irregular bars or availability order")
    if asof_ms - closes[-1] > 2 * interval:
        raise ValueError("stale research input")
    costs = CostBudget(**_record(payload["costs"], "costs"))
    costs.validate(asof_ms)
    if series["market"].split("_")[-1] != costs.quote_currency:
        raise ValueError("cost quote currency differs from market")

    if set(packet) != {"schema_version", "forecast", "opportunities", "coordination",
                       "live_authority", "automatic_promotion", "packet_sha256"}:
        raise ValueError("unexpected alpha packet fields")
    unhashed = {key: value for key, value in packet.items() if key != "packet_sha256"}
    if packet["packet_sha256"] != sha256(_canonical(unhashed)).hexdigest():
        raise ValueError("component packet digest differs")
    if (packet["schema_version"] != "1.0" or packet["live_authority"] is not False or
            packet["automatic_promotion"] is not False):
        raise ValueError("alpha packet cannot grant financial or promotion authority")
    forecast = _record(packet["forecast"], "forecast")
    coordinate = _record(packet["coordination"], "coordination")
    if (coordinate.get("input_snapshot_id") != snapshot_id or
            coordinate.get("correlation_group") != "SAME_PRICE_HISTORY" or
            coordinate.get("independent_votes") is not False or
            coordinate.get("broker_keys_exposed") is not False or
            coordinate.get("refresh_policy") != "EXPLICIT_OFFLINE_RESEARCH_TRIGGER"):
        raise ValueError("alpha correlation and explicit-refresh contract differs")
    for name, expected in (("snapshot_id", snapshot_id), ("asof_ms", asof_ms),
                           ("market", series["market"]), ("interval_ms", interval),
                           ("source_ref", series["source_ref"]),
                           ("evidence_class", series["evidence_class"]),
                           ("reference_close_at_ms", closes[-1])):
        if type(forecast.get(name)) is not type(expected) or forecast.get(name) != expected:
            raise ValueError("forecast differs from its bound input")
    input_hash = sha256(json.dumps({"prices": prices, "times": closes,
                                   "availability": available},
                                  separators=(",", ":")).encode()).hexdigest()
    if forecast.get("input_sha256") != input_hash:
        raise ValueError("forecast source digest differs")
    if (forecast.get("schema_version") != "1.0" or forecast.get("provider") != "WHITEBIT" or
            forecast.get("mode") != "SHADOW_RESEARCH_ONLY" or
            forecast.get("return_reference") != "LAST_OBSERVED_CLOSE_NOT_EXECUTABLE_PRICE" or
            any(forecast.get(key) is not False for key in
                ("live_authority", "automatic_promotion", "whitebit_profitability_proven"))):
        raise ValueError("forecast is not the supported advisory research contract")
    horizon = forecast.get("horizon_bars")
    if (type(horizon) is not int or not 1 <= horizon <= 24 or
            forecast.get("forecast_target_close_at_ms") != closes[-1] + horizon * interval or
            forecast["forecast_target_close_at_ms"] <= asof_ms):
        raise ValueError("forecast horizon is elapsed or inconsistent")
    models = _record(forecast.get("models"), "models")
    members = set(models) - {"FIXED_ENSEMBLE"}
    if not members or not members <= _BACKENDS:
        raise ValueError("unsupported bounded alpha model set")
    if len(members) == 1:
        if set(models) != members:
            raise ValueError("single backend cannot add an ensemble vote")
        selected = next(iter(members))
    else:
        ensemble = _record(models.get("FIXED_ENSEMBLE"), "fixed ensemble")
        if (ensemble.get("weights") != "EQUAL_FIXED_BEFORE_HOLDOUT" or
                type(ensemble.get("members")) is not list or
                len(ensemble["members"]) != len(members) or
                set(ensemble["members"]) != members):
            raise ValueError("fixed ensemble membership differs")
        selected = "FIXED_ENSEMBLE"
    opportunities = _record(packet["opportunities"], "opportunities")
    if set(opportunities) != set(models):
        raise ValueError("model/opportunity set differs")
    for name, raw_model in models.items():
        model = _record(raw_model, "model")
        if (model.get("coverage_guaranteed") is not False or
                model.get("holdout_used_for_model_selection") is not False):
            raise ValueError("research uncertainty cannot be promoted to a guarantee")
        expected = opportunity(model["gross_midpoint_return_ratio"], model["lower_return_ratio"],
                               costs, market=series["market"], asof_ms=asof_ms)
        if _canonical(opportunities[name]) != _canonical(expected):
            raise ValueError("after-cost opportunity differs from bound assumptions")
    return series, costs, selected, opportunities[selected]


def _reference(manifest: dict) -> str:
    return f"artifact:{manifest['artifact_id']}@{manifest['sha256']}"


def _read(reader, reference: str, kind: str):
    _text(reference, "artifact receipt")
    prefix, separator, digest = reference.partition("@sha256:")
    identifier = prefix.removeprefix("artifact:")
    if (not prefix.startswith("artifact:") or not separator or str(UUID(identifier)) != identifier or
            len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest)):
        raise ValueError("canonical immutable artifact receipt required")
    manifest, data = reader(identifier)
    if (manifest["sha256"] != "sha256:" + digest or len(data) > MAX_DOCUMENT_BYTES or
            manifest.get("media_type") != "application/json" or
            manifest.get("metadata", {}).get("artifact_kind") != kind):
        raise ValueError("artifact digest, kind or bounded JSON contract differs")
    return manifest, _record(strict_json_loads(data.decode("utf-8")), "artifact document")


def _publish(store, document, *, kind, sources=(), metadata=None):
    data = _canonical(document)
    identity = "autotrade:alpha:" + kind + ":" + ":".join(sources) + ":" + sha256(data).hexdigest()
    return store.publish_bytes(artifact_id=str(uuid5(NAMESPACE_URL, identity)), data=data,
        media_type="application/json", rights={"storage": True, "export": False},
        source_refs=list(sources), metadata={"artifact_kind": kind, **(metadata or {})})


def _aggregate(store, reader, packet_ref, *, expected_snapshot_id, asof_ms):
    packet_manifest, envelope = _read(reader, packet_ref, "ALPHA_RESEARCH_PACKET")
    if set(envelope) != {"schema_version", "input_ref", "packet"} or envelope["schema_version"] != "1.0":
        raise ValueError("unsupported alpha artifact envelope")
    input_ref = envelope["input_ref"]
    packet = _record(envelope["packet"], "packet")
    input_manifest, payload = _read(reader, input_ref, "ALPHA_RESEARCH_INPUT")
    if (packet_manifest["source_refs"] != [input_ref] or input_manifest["source_refs"] or
            packet_manifest["metadata"].get("input_ref") != input_ref):
        raise ValueError("alpha source receipt chain differs")
    series, costs, selected, candidate = _validate(payload, packet,
        snapshot_id=expected_snapshot_id, asof_ms=asof_ms)
    references = (input_ref, packet_ref)
    # The already-fitted fixed ensemble contributes once. Individual members
    # do not add votes. Weight 1 is a fixed DAG weight, not a probability claim.
    score = min(Decimal("1"), Decimal(candidate["lower_net_return_ratio"])) if candidate["candidate"] else Decimal("0")
    role = "whitebit-alpha-shared"
    specs = (SpecialistSpec(role, "SAME_PRICE_HISTORY", Decimal("0"), Decimal("1"),
                            required_inputs=references),)
    plan = plan_specialists(specs, input_snapshot_id=expected_snapshot_id,
                            available_inputs=references, total_budget="0")
    # Use actual immutable publication time; never backdate model completion to
    # a historical market cutoff. This is an offline proposal, not live admission.
    completed = datetime.fromisoformat(packet_manifest["created_at"].replace("Z", "+00:00"))
    run = SpecialistRun(role, expected_snapshot_id, "LONG" if score > 0 else "FLAT",
                        score, Decimal("1"), references, Decimal("0"), completed)
    aggregate = aggregate_specialists(specs, (run,), plan=plan,
        input_snapshot_id=expected_snapshot_id, available_inputs=references,
        total_budget="0", decision_deadline=completed)
    result = {"schema_version": "1.0", "mode": "SHADOW_RESEARCH_ONLY",
        "input_snapshot_id": expected_snapshot_id, "research_asof_ms": asof_ms,
        "completed_at": packet_manifest["created_at"], "selected_report": selected,
        "direction": aggregate.direction, "score": str(aggregate.score),
        "accepted_roles": list(aggregate.accepted_roles),
        "rejected_roles": [list(row) for row in aggregate.rejected_roles],
        "evidence_refs": list(aggregate.evidence_refs),
        "correlation_group": "SAME_PRICE_HISTORY", "independent_votes": False,
        "weight_semantics": "FIXED_GROUP_WEIGHT_NOT_PROBABILITY",
        "additional_model_calls": 0, "aggregation_cost": str(aggregate.total_cost),
        "research_model_cost_quote": costs.model_cost_quote, "quote_currency": costs.quote_currency,
        "evidence_class": series["evidence_class"], "provider_source_authenticated": False,
        "financial_admission": "NOT_PERFORMED", "automatic_promotion": False,
        "live_authority_granted": aggregate.live_authority_granted,
        "profitability_proven": False}
    result_manifest = _publish(store, result, kind="ALPHA_SPECIALIST_DAG_RESULT", sources=references)
    result_ref = _reference(result_manifest)
    _manifest, durable_result = _read(reader, result_ref, "ALPHA_SPECIALIST_DAG_RESULT")
    if durable_result != result:
        raise ValueError("durable alpha DAG result differs")
    return {"input_ref": input_ref, "packet_ref": packet_ref,
            "result_ref": result_ref, "result": durable_result}


def publish_alpha_research(artifact_root: str | Path, payload: dict, packet: dict) -> dict:
    """Persist one completed fit and consume its authenticated receipt in the DAG."""
    payload = strict_json_loads(_canonical(payload).decode("utf-8"))
    packet = strict_json_loads(_canonical(packet).decode("utf-8"))
    series = _record(payload.get("series"), "series")
    snapshot_id, asof_ms = series.get("snapshot_id"), series.get("asof_ms")
    _validate(payload, packet, snapshot_id=snapshot_id, asof_ms=asof_ms)
    store = ArtifactStore(artifact_root)
    reader = trusted_authenticated_reader(artifact_root, publication_store=store)
    input_ref = _reference(_publish(store, payload, kind="ALPHA_RESEARCH_INPUT"))
    envelope = {"schema_version": "1.0", "input_ref": input_ref, "packet": packet}
    packet_ref = _reference(_publish(store, envelope, kind="ALPHA_RESEARCH_PACKET",
                            sources=(input_ref,), metadata={"input_ref": input_ref}))
    return _aggregate(store, reader, packet_ref, expected_snapshot_id=snapshot_id, asof_ms=asof_ms)


def aggregate_alpha_receipt(artifact_root: str | Path, packet_ref: str, *,
                           expected_snapshot_id: str, asof_ms: int) -> dict:
    """Restart from immutable receipts without importing or fitting model backends."""
    store = ArtifactStore(artifact_root)
    reader = trusted_authenticated_reader(artifact_root, publication_store=store)
    return _aggregate(store, reader, packet_ref,
                      expected_snapshot_id=expected_snapshot_id, asof_ms=asof_ms)
