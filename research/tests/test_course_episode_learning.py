from research.autotrade_research.learning.course_episode_learning import analyze_course_episodes


def episode(delay, pnl):
    return {"pnl_after_costs": str(pnl), "fees": "0.1", "funding": "0.01",
        "profitable": pnl > 0, "exit_reason": "TARGET" if pnl > 0 else "STOP",
        "sweep_to_shift_bars": delay}


def test_series_stays_frozen_before_one_hundred_cycles():
    value = analyze_course_episodes([{"market": "BTC_PERP",
        "dataset_identity_sha256": "a" * 64,
        "episodes": [episode(2, 1) for _ in range(99)]}])
    assert value["status"] == "COLLECTING_FROZEN_SERIES"
    assert value["candidate_hypothesis"] is None
    assert value["strategy_changed"] is False


def test_registers_one_axis_hypothesis_without_promotion():
    rows = ([episode(2, -1) for _ in range(40)]
            + [episode(6, 2) for _ in range(30)]
            + [episode(12, -2) for _ in range(30)])
    value = analyze_course_episodes([{"market": "BTC_PERP",
        "dataset_identity_sha256": "b" * 64, "episodes": rows}])
    assert value["status"] == "ONE_AXIS_CANDIDATE_REGISTERED"
    assert value["candidate_hypothesis"]["candidate_minimum_shift_confirmation_bars"] == 5
    assert value["candidate_hypothesis"]["candidate_shift_expiry_bars"] == 8
    assert value["automatic_promotion"] is False
    assert value["development_consumed"] is False

