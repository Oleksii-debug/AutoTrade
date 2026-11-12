import pytest

from tools.run_whitebit_maker_shadow import _record_quote_admission


def test_quote_admission_counts_survive_state_updates():
    state = _record_quote_admission({}, {
        "admitted": False, "reason": "SPREAD_BELOW_HURDLE",
    })
    state = _record_quote_admission(state, {
        "admitted": False, "reason": "DIRECTIONAL_FLOW",
    })
    state = _record_quote_admission(state, {
        "admitted": True, "reason": "ADMITTED",
    })

    assert state["quote_admission_attempts"] == 3
    assert state["quote_admitted_count"] == 1
    assert state["quote_admission_counts"] == {
        "ADMITTED": 1,
        "DIRECTIONAL_FLOW": 1,
        "SPREAD_BELOW_HURDLE": 1,
    }


@pytest.mark.parametrize("admission", [
    {"admitted": "yes", "reason": "ADMITTED"},
    {"admitted": False, "reason": ""},
    {"admitted": False},
])
def test_quote_admission_counter_rejects_malformed_results(admission):
    with pytest.raises(ValueError, match="admission result"):
        _record_quote_admission({}, admission)

