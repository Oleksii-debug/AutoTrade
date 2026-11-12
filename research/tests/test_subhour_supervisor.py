from tools.run_whitebit_subhour_supervisor import FROZEN_STRATEGY_ID, configurations
from pathlib import Path


def test_supervisor_uses_explicit_absolute_profit_gate():
    source = Path("tools/run_whitebit_subhour_supervisor.py").read_text(encoding="utf-8")
    assert 'gate_policy="POSITIVE_AFTER_COSTS"' in source


def test_registry_is_small_and_never_exceeds_one_hour():
    values = configurations()
    assert len(values) == 1
    assert {value.maximum_hold_bars for value in values} <= {2, 3, 4}
    assert {value.minimum_hold_bars for value in values} <= {2, 3, 4}
    assert all(value.stop_atr == "1" and value.take_profit_atr == "4"
               for value in values)
    assert all(value.maximum_entries_per_utc_day == 1 for value in values)
    assert all(value.minimum_expected_move_fraction == "0.006" for value in values)
    assert FROZEN_STRATEGY_ID == "SOL_BOLLINGER_RSI_60M_V1"

