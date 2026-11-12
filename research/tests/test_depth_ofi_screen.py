from datetime import datetime, timezone, timedelta
from pathlib import Path

from research.autotrade_research.integrations.whitebit_depth import DepthBookObservation
from research.autotrade_research.learning.whitebit_depth_tape import WhiteBitDepthTapeStore
from research.autotrade_research.strategies.depth_ofi_screen import (
    candidates, screen_depth_ofi,
)


def build_tape(path: Path, count: int = 360) -> None:
    store = WhiteBitDepthTapeStore(path, bucket_seconds=1, retention_days=3)
    start = datetime.now(timezone.utc) + timedelta(seconds=1)
    for index in range(count):
        at = start + timedelta(seconds=index)
        price = 100 + index / 1000
        store.ingest(DepthBookObservation(market="BTC_PERP", received_at=at,
            generation=1, update_id=index + 1, past_update_id=index or None,
            snapshot=index == 0, bid_price=str(price - .005), ask_price=str(price + .005),
            mid_price=str(price), spread_bps="1", bid_depth_top5="6",
            ask_depth_top5="4", imbalance_top5="0.2",
            bid_quantity_change="1", ask_quantity_change="-1",
            order_flow_imbalance="2", source_sha256="sha256:" + "c" * 64))
    store.close()


def test_registry_is_deliberately_small():
    assert len(candidates()) == 4


def test_screen_waits_for_predeclared_causal_sample(tmp_path):
    path = tmp_path / "depth.sqlite3"; build_tape(path)
    value = screen_depth_ofi(path, "BTC_PERP", minimum_buckets=600)
    assert value["status"] == "WAITING_FOR_CAUSAL_BUCKETS"
    assert value["candidate_count"] == 4
    assert value["final_consumed"] is False


def test_locked_final_is_not_read_when_train_fails_after_costs(tmp_path):
    path = tmp_path / "depth.sqlite3"; build_tape(path)
    value = screen_depth_ofi(path, "BTC_PERP", minimum_buckets=300,
        round_trip_cost_bps="100")
    assert value["status"] == "REJECTED"
    assert value["final_consumed"] is False
    assert value["prospective_qualification"] is False
    assert value["network_orders"] is False

