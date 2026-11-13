from datetime import datetime, timezone

from research.autotrade_research.integrations.whitebit_depth import (
    DepthLocalBook,
    parse_depth_message,
)


def test_depth_observation_preserves_provider_message_time_and_receipt_time():
    raw = (
        '{"id":null,"method":"depth_update","params":[true,'
        '{"asks":[["101","2"]],"bids":[["99","3"]],'
        '"timestamp":1794484798.25,"event_time":1794484799.1234567,"update_id":10},'
        '"BTC_USDT"]}'
    )
    receipt = datetime(2026, 11, 12, 12, 0, 2, tzinfo=timezone.utc)
    update = parse_depth_message(raw, market="BTC_USDT", received_at=receipt)
    observation = DepthLocalBook("BTC_USDT", limit=5).apply(update, generation=1)

    assert observation.message_time == datetime(2026, 11, 12, 11, 59, 59, 123456, timezone.utc)
    assert observation.message_time is update.message_time
    assert observation.received_at == receipt
