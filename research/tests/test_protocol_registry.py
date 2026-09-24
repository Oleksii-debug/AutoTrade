from __future__ import annotations

import sqlite3

import pytest

from autotrade_research.science import (
    ProtocolConflict,
    ProtocolViolation,
    ScientificRegistry,
)


def protocol() -> dict:
    return {
        "hypothesis": "candidate improves net utility over baseline",
        "strategy": {"name": "deterministic-baseline"},
        "features": ["return_5m"],
        "search_space": {"threshold": ["0.01", "0.02"]},
        "train_period": {"start": "2024-01-01", "end": "2024-12-31"},
        "validation_period": {"start": "2025-01-01", "end": "2025-06-30"},
        "test_period": {"start": "2025-07-01", "end": "2025-12-31"},
        "forward_period": {"start": "2026-01-01", "end": "2026-06-30"},
        "labels": ["net_return_after_cost"],
        "horizons": ["1d"],
        "purge_embargo": {"purge": "1d", "embargo": "1d"},
        "universe": ["BTC-USD"],
        "cost_fill_model": {"fee_bps": "10"},
        "baselines": ["cash", "existing_champion"],
        "primary_metrics": [{"name": "net_utility", "direction": "max"}],
        "secondary_metrics": [{"name": "drawdown", "direction": "min"}],
        "trial_budget": 2,
        "stopping_rules": {"max_failures": 2},
        "statistical_estimator": {"name": "block_bootstrap"},
        "multiplicity_treatment": {"method": "holm"},
        "minimum_practical_effect": "0.015",
        "risk_constraints": {"max_drawdown": "0.20"},
        "retention_tolerances": {"max_degradation": "0.02"},
        "promotion_rule": {"lower_bound_gt": "0.015"},
    }


def test_protocol_identity_is_immutable_and_idempotent(tmp_path):
    registry = ScientificRegistry(tmp_path / "science.sqlite3")
    protocol_id = "00000000-0000-0000-0000-000000000001"
    first = registry.register_protocol(protocol(), protocol_id=protocol_id)
    second = registry.register_protocol(protocol(), protocol_id=protocol_id)
    assert first.protocol_hash == second.protocol_hash

    changed = protocol()
    changed["minimum_practical_effect"] = "0.001"
    with pytest.raises(ProtocolConflict, match="immutable"):
        registry.register_protocol(changed, protocol_id=protocol_id)


def test_binary_float_is_rejected_from_frozen_scientific_evidence(tmp_path):
    registry = ScientificRegistry(tmp_path / "science.sqlite3")
    bad = protocol()
    bad["minimum_practical_effect"] = 0.015
    with pytest.raises(ProtocolViolation, match="binary float"):
        registry.register_protocol(bad)


def test_failed_and_discarded_trials_consume_registered_budget(tmp_path):
    registry = ScientificRegistry(tmp_path / "science.sqlite3")
    registered = registry.register_protocol(protocol())
    registry.record_trial(
        registered.protocol_id,
        status="FAILED",
        payload={"reason": "fit_failed"},
    )
    registry.record_trial(
        registered.protocol_id,
        status="DISCARDED",
        payload={"reason": "invalid_candidate"},
    )
    completeness = registry.completeness(registered.protocol_id)
    assert completeness["recorded_trials"] == 2
    assert completeness["remaining_trial_budget"] == 0
    assert completeness["includes_non_successes"] is True

    with pytest.raises(ProtocolViolation, match="budget exhausted"):
        registry.record_trial(
            registered.protocol_id,
            status="COMPLETED",
            payload={"metric": "0.02"},
        )


def test_repeated_holdout_use_cannot_remain_untouched(tmp_path):
    registry = ScientificRegistry(tmp_path / "science.sqlite3")
    registered = registry.register_protocol(protocol())
    holdout = "forward-2026-h1"

    first = registry.register_evaluation(
        registered.protocol_id,
        holdout_id=holdout,
        result={"net_utility": "0.020"},
    )
    assert first["prior_access_count"] == 0
    assert first["untouched"] == 1
    assert registry.holdout_access_count(registered.protocol_id, holdout) == 1

    second = registry.register_evaluation(
        registered.protocol_id,
        holdout_id=holdout,
        result={"net_utility": "0.021"},
    )
    assert second["prior_access_count"] == 1
    assert second["untouched"] == 0
    assert registry.holdout_access_count(registered.protocol_id, holdout) == 2


def test_manual_holdout_access_contaminates_later_locked_evaluation(tmp_path):
    registry = ScientificRegistry(tmp_path / "science.sqlite3")
    registered = registry.register_protocol(protocol())
    holdout = "forward-2026-h1"
    registry.record_holdout_access(
        registered.protocol_id,
        holdout_id=holdout,
        purpose="parameter_selection",
    )
    evaluation = registry.register_evaluation(
        registered.protocol_id,
        holdout_id=holdout,
        result={"net_utility": "0.019"},
    )
    assert evaluation["prior_access_count"] == 1
    assert evaluation["untouched"] == 0


def test_database_triggers_block_destructive_rewrites(tmp_path):
    path = tmp_path / "science.sqlite3"
    registry = ScientificRegistry(path)
    registered = registry.register_protocol(protocol())

    con = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute(
                "UPDATE protocols SET payload_json='{}' WHERE protocol_id=?",
                (registered.protocol_id,),
            )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute(
                "DELETE FROM protocols WHERE protocol_id=?",
                (registered.protocol_id,),
            )
    finally:
        con.close()


def test_registry_state_survives_restart(tmp_path):
    path = tmp_path / "science.sqlite3"
    first = ScientificRegistry(path)
    registered = first.register_protocol(protocol())
    first.record_trial(
        registered.protocol_id,
        status="FAILED",
        payload={"reason": "fit_failed"},
    )

    reopened = ScientificRegistry(path)
    completeness = reopened.completeness(registered.protocol_id)
    assert completeness["recorded_trials"] == 1
    assert completeness["statuses"] == {"FAILED": 1}
