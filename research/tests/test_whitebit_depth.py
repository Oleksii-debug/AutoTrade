from datetime import datetime, timezone
import json

import pytest

from research.autotrade_research.integrations.whitebit_depth import (
    DepthBookObservation, DepthLocalBook, DepthSequenceGap, parse_depth_message,
)
from research.autotrade_research.learning.whitebit_depth_tape import (
    WhiteBitDepthTapeStore,
)


NOW=datetime(2026,11,12,12,tzinfo=timezone.utc)


def frame(snapshot=True,past=None,update=10):
    body={"asks":[["101","2"]],"bids":[["99","3"]],
        "timestamp":1794484800,"event_time":1794484800,"update_id":update}
    if past is not None: body["past_update_id"]=past
    return json.dumps({"id":None,"method":"depth_update",
        "params":[snapshot,body,"BTC_USDT"]})


def test_snapshot_and_causal_delta_build_book():
    book=DepthLocalBook("BTC_USDT",limit=5)
    first=parse_depth_message(frame(),market="BTC_USDT",received_at=NOW)
    observed=book.apply(first,generation=1)
    assert observed.imbalance_top5=="0.2"
    delta=parse_depth_message(frame(False,10,11),market="BTC_USDT",received_at=NOW)
    observed=book.apply(delta,generation=1)
    assert observed.update_id==11


def test_gap_fails_and_requires_fresh_snapshot():
    book=DepthLocalBook("BTC_USDT",limit=5)
    book.apply(parse_depth_message(frame(),market="BTC_USDT",received_at=NOW),generation=1)
    bad=parse_depth_message(frame(False,9,11),market="BTC_USDT",received_at=NOW)
    with pytest.raises(DepthSequenceGap): book.apply(bad,generation=1)


def test_zero_quantity_removes_level_and_control_is_bounded():
    book=DepthLocalBook("BTC_USDT",limit=5)
    book.apply(parse_depth_message(frame(),market="BTC_USDT",received_at=NOW),generation=1)
    raw=json.loads(frame(False,10,11)); raw["params"][1]["asks"]=[["101","0"],["102","1"]]
    value=parse_depth_message(json.dumps(raw),market="BTC_USDT",received_at=NOW)
    observed=book.apply(value,generation=1)
    assert observed.ask_price=="102"
    ack=parse_depth_message(json.dumps({"id":7,"result":{"status":"success"},"error":None}),
        market="BTC_USDT",received_at=NOW)
    assert ack.code=="SUBSCRIBED"


def test_depth_tape_is_restart_safe_and_public_only(tmp_path):
    path = tmp_path / "depth.sqlite3"
    value = DepthBookObservation(
        market="BTC_USDT", received_at=NOW, generation=1, update_id=10,
        past_update_id=None, snapshot=True, bid_price="99", ask_price="101",
        mid_price="100", spread_bps="200", bid_depth_top5="3",
        ask_depth_top5="2", imbalance_top5="0.2",
        bid_quantity_change="0", ask_quantity_change="0",
        order_flow_imbalance="2",
        source_sha256="sha256:" + "a" * 64,
    )
    first = WhiteBitDepthTapeStore(path, bucket_seconds=1, retention_days=3)
    first.ingest(value)
    first.close()
    second = WhiteBitDepthTapeStore(path, bucket_seconds=1, retention_days=3)
    status = second.status()
    second.close()
    assert status["bucket_count"] == 1
    assert status["market_bucket_counts"] == {"BTC_USDT": 1}
    assert status["public_data_only"] is True
    assert status["trading_authority"] is False


def test_causal_ofi_uses_best_quote_transition():
    book = DepthLocalBook("BTC_USDT", limit=5)
    book.apply(parse_depth_message(frame(), market="BTC_USDT", received_at=NOW),
               generation=1)
    raw = json.loads(frame(False, 10, 11))
    raw["params"][1]["bids"] = [["99", "5"]]
    raw["params"][1]["asks"] = [["101", "1"]]
    observed = book.apply(parse_depth_message(json.dumps(raw), market="BTC_USDT",
        received_at=NOW), generation=1)
    assert observed.order_flow_imbalance == "3"


def test_persistent_l2_pressure_confirms_only_matching_direction(tmp_path):
    path = tmp_path / "pressure.sqlite3"
    store = WhiteBitDepthTapeStore(path, bucket_seconds=1, retention_days=3)
    for offset in range(10):
        store.ingest(DepthBookObservation(
            market="BTC_USDT", received_at=NOW.replace(second=offset), generation=1,
            update_id=offset + 1, past_update_id=offset or None,
            snapshot=offset == 0, bid_price="99", ask_price="99.01",
            mid_price="99.005", spread_bps="1", bid_depth_top5="6",
            ask_depth_top5="4", imbalance_top5="0.2",
            bid_quantity_change="1", ask_quantity_change="-1",
            order_flow_imbalance="2", source_sha256="sha256:" + "b" * 64))
    now_epoch = int(NOW.replace(second=9).timestamp())
    long = store.microstructure_confirmation("BTC_USDT", "LONG", now_epoch=now_epoch)
    short = store.microstructure_confirmation("BTC_USDT", "SHORT", now_epoch=now_epoch)
    store.close()
    assert long["status"] == "CONFIRMED"
    assert short["status"] == "CONFLICT"


