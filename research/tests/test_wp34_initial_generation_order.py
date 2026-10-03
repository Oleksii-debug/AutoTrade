from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from autotrade_research.features.causal import SourceValue, rolling_return


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _source(identity: str, value: str, *, source_sequence: int) -> SourceValue:
    return SourceValue.create(
        observation_id=f"{identity}@r1",
        symbol="AAA",
        event_time=BASE,
        available_at=BASE + timedelta(seconds=1),
        value=value,
        source_revision="1",
        source_identity=identity,
        source_sequence=source_sequence,
    )


class CanonicalSourceSequenceOrderTests(unittest.TestCase):
    def test_generic_source_values_use_explicit_canonical_sequence_order(self):
        earlier = _source("event-a", "100", source_sequence=10)
        later = _source("event-b", "110", source_sequence=11)

        point = rolling_return(
            [later, earlier],
            symbol="AAA",
            decision_time=BASE + timedelta(seconds=1),
            count=2,
        )

        self.assertEqual(point.input_ids, ("event-a@r1", "event-b@r1"))
        self.assertEqual(point.value, Decimal("0.1"))


if __name__ == "__main__":
    unittest.main()
