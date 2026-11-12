from research.autotrade_research.learning.course_doctrine import curriculum_packet, criticize_setup


def valid_setup():
    return {"regime": "TREND", "direction": "LONG", "timeframes": [1440, 60, 15],
        "htf_bias": "LONG", "mtf_poi": "FVG", "ltf_trigger": "QUASIMODO",
        "location": "DISCOUNT", "risk_fraction": "0.01", "reward_risk": "2.5",
        "stop_beyond_invalidation": True, "stop_widening_allowed": False,
        "fibonacci_used": True, "fibonacci_entry": "0.705"}


def test_complete_course_setup_passes_critic():
    assert criticize_setup(valid_setup()) == []
    packet = curriculum_packet()
    assert len(packet["source_digests"]) == 10
    assert packet["transcript_text_embedded"] is False


def test_missing_top_down_and_risk_fail_closed():
    value = valid_setup(); value["timeframes"] = [1440, 5, 1]
    value["risk_fraction"] = "0.02"; value["location"] = "PREMIUM"
    violations = criticize_setup(value)
    assert "TIMEFRAME_RATIO_TOO_LARGE" in violations
    assert "RISK_EXCEEDS_ONE_PERCENT" in violations
    assert "LONG_NOT_IN_DISCOUNT" in violations

