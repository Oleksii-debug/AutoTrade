from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.corporate_action_accounting import _exact_utc_instant
from mvp.autotrade_mvp.corporate_actions import (
    CorporateEvent,
    EquityState,
    accrue_borrow_financing,
)


class CorporateActionIngressTrustTests(unittest.TestCase):
    def _event(self, **overrides):
        values = {
            "event_id": "event-1",
            "instrument_id": "instrument-1",
            "instrument_version": 1,
            "kind": "CASH_DIVIDEND",
            "effective_date": date(2026, 1, 2),
            "source_revision": "revision-1",
            "payload": {"per_share": "1", "currency": "USD"},
            "source_sequence": 0,
        }
        values.update(overrides)
        return CorporateEvent.create(**values)

    def test_payload_mapping_subclass_is_rejected_before_items_dispatch(self):
        class HostileDict(dict):
            def items(self):
                raise AssertionError("hostile mapping items dispatch")

        payload = HostileDict(per_share="1", currency="USD")
        with self.assertRaisesRegex(TypeError, "exact dict"):
            self._event(payload=payload)

        with self.assertRaisesRegex(TypeError, "exact dict"):
            CorporateEvent(
                event_id="event-direct",
                instrument_id="instrument-1",
                instrument_version=1,
                kind="CASH_DIVIDEND",
                effective_date=date(2026, 1, 2),
                source_revision="revision-1",
                payload=payload,
                source_sequence=0,
            )

    def test_integer_subclasses_are_rejected_before_comparison_dispatch(self):
        class HostileInt(int):
            def __lt__(self, other):
                raise AssertionError("hostile integer comparison")

            def __str__(self):
                raise AssertionError("hostile integer stringification")

        hostile_one = HostileInt(1)
        with self.assertRaisesRegex(ValueError, "instrument_version"):
            self._event(instrument_version=hostile_one)

        with self.assertRaisesRegex(ValueError, "source_sequence"):
            self._event(source_sequence=hostile_one)

    def test_numeric_payload_is_bounded_before_stringification(self):
        oversized_decimal = Decimal("1e100000")
        with self.assertRaisesRegex(ValueError, "resource envelope"):
            self._event(
                payload={"per_share": oversized_decimal, "currency": "USD"},
            )

        with self.assertRaisesRegex(ValueError, "resource envelope"):
            CorporateEvent(
                event_id="event-direct",
                instrument_id="instrument-1",
                instrument_version=1,
                kind="CASH_DIVIDEND",
                effective_date=date(2026, 1, 2),
                source_revision="revision-1",
                payload={"per_share": oversized_decimal, "currency": "USD"},
                source_sequence=0,
            )

        oversized_int = 10 ** 5000
        with self.assertRaisesRegex(ValueError, "resource envelope"):
            self._event(
                payload={"per_share": oversized_int, "currency": "USD"},
            )

    def test_financing_days_reject_integer_subclasses(self):
        class HostileInt(int):
            def __lt__(self, other):
                raise AssertionError("hostile integer comparison")

        state = EquityState.create(
            symbol="AAA",
            quantity="0",
            total_basis="0",
            settled_cash="0",
            currency="USD",
        )
        with self.assertRaisesRegex(ValueError, "days"):
            accrue_borrow_financing(
                state,
                daily_rate="0",
                marked_value="0",
                days=HostileInt(1),
            )

    def test_activation_instants_reject_datetime_subclasses_before_dispatch(self):
        class HostileDatetime(datetime):
            @property
            def tzinfo(self):
                raise AssertionError("hostile datetime tzinfo dispatch")

            def astimezone(self, tz=None):
                raise AssertionError("hostile datetime astimezone dispatch")

        hostile = HostileDatetime(2026, 1, 2, tzinfo=timezone.utc)
        with self.assertRaisesRegex(TypeError, "exact datetime"):
            _exact_utc_instant(hostile, name="activation_at")

    def test_activation_instants_reject_custom_tzinfo_before_callbacks(self):
        class HostileTimezone(tzinfo):
            def utcoffset(self, dt):
                raise AssertionError("hostile tzinfo utcoffset dispatch")

            def dst(self, dt):
                raise AssertionError("hostile tzinfo dst dispatch")

        hostile_tz = HostileTimezone()
        value = datetime(2026, 1, 2, tzinfo=hostile_tz)
        with self.assertRaisesRegex(TypeError, "fixed built-in timezone"):
            _exact_utc_instant(value, name="activation_cut")

    def test_activation_instants_preserve_fixed_builtin_timezone_support(self):
        fixed = timezone(timedelta(hours=2))
        value = datetime(2026, 1, 2, 2, 30, tzinfo=fixed)
        self.assertEqual(
            _exact_utc_instant(value, name="activation_at"),
            datetime(2026, 1, 2, 0, 30, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
