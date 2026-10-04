"""Restart equivalence and fail-closed restoration of canonical funding state."""
from copy import deepcopy
from decimal import Decimal, Inexact, Rounded, ROUND_CEILING, ROUND_FLOOR, localcontext
import json
import unittest

from mvp.autotrade_mvp.perpetuals import FundingLedger, PerpetualError


class PerpetualFundingStateTests(unittest.TestCase):
    def apply(self, ledger, event_id, period, amount, currency="USD", instrument="PERP@1"):
        return ledger.apply(event_id=event_id,funding_period_id=period,
                            instrument_id=instrument,currency=currency,amount=amount)

    def initial(self):
        ledger=FundingLedger()
        self.apply(ledger,"one","p1","1000000000000000000000000000000")
        self.apply(ledger,"alias-one","p1","1e30")
        self.apply(ledger,"two","p2","1e-30")
        self.apply(ledger,"btc","p1","-0.00000001","BTC","INVERSE@1")
        return ledger

    def test_uninterrupted_equals_serialized_restart_continue_and_duplicate(self):
        for precision,rounding in ((1,ROUND_CEILING),(8,ROUND_FLOOR)):
            with self.subTest(precision=precision,rounding=rounding),localcontext() as context:
                context.prec=precision;context.rounding=rounding
                context.traps[Inexact]=True;context.traps[Rounded]=True
                uninterrupted=self.initial()
                state=json.loads(json.dumps(uninterrupted.export_state(),sort_keys=True))
                restored=FundingLedger.from_state(state)
                self.assertEqual(restored.export_state(),uninterrupted.export_state())
                for ledger in (uninterrupted,restored):
                    self.apply(ledger,"third","p3","-1e30")
                    self.apply(ledger,"alias-two","p2","1e-30")
                    self.apply(ledger,"btc-next","p2","0.00000002","BTC","INVERSE@1")
                    self.apply(ledger,"one","p1","1e30")
                self.assertEqual(restored.export_state(),uninterrupted.export_state())
                self.assertEqual(restored.balance("USD"),Decimal("1e-30"))
                self.assertEqual(restored.balance("BTC"),Decimal("0.00000001"))

    def test_state_and_exported_rows_are_detached_from_retained_state(self):
        original=self.initial();state=original.export_state()
        restored=FundingLedger.from_state(state)
        expected=restored.export_state()
        state["events"][0]["amount"]="999"
        state["balances"]["USD"]="999"
        self.assertEqual(original.export_state(),expected)
        self.assertEqual(restored.export_state(),expected)

    def test_missing_components_bad_version_duplicates_and_balance_corruption_fail(self):
        state=self.initial().export_state()
        variants=[]
        for key in ("events","balances","schema_version"):
            candidate=deepcopy(state);candidate.pop(key);variants.append(candidate)
        candidate=deepcopy(state);candidate["schema_version"]="funding-ledger@2";variants.append(candidate)
        class HostileVersion(str):
            def __eq__(self, other):
                raise AssertionError("virtual schema comparison")
        candidate=deepcopy(state);candidate["schema_version"]=HostileVersion("funding-ledger@1");variants.append(candidate)
        candidate=deepcopy(state);candidate["events"].append(deepcopy(candidate["events"][0]));variants.append(candidate)
        candidate=deepcopy(state);candidate["balances"]["USD"]="999";variants.append(candidate)
        candidate=deepcopy(state);candidate["events"].pop(2);variants.append(candidate)
        candidate=deepcopy(state);candidate["events"][0]["currency"]="BTC";variants.append(candidate)
        candidate=deepcopy(state);candidate["events"][0]["amount"]="1.0e30";variants.append(candidate)
        for index,candidate in enumerate(variants):
            with self.subTest(index=index),self.assertRaises(PerpetualError):
                FundingLedger.from_state(candidate)

    def test_conflicting_period_alias_after_restart_does_not_mutate(self):
        restored=FundingLedger.from_state(self.initial().export_state())
        before=restored.export_state()
        with self.assertRaisesRegex(PerpetualError,"different economic content"):
            self.apply(restored,"alias-one-changed","p1","999")
        self.assertEqual(restored.export_state(),before)

    def test_bounded_accumulation_preserves_enrollment_order_across_restart(self):
        ledger=FundingLedger();large="9"*256
        self.apply(ledger,"z-first","p1",large)
        self.apply(ledger,"a-offset","p2","-"+large)
        self.apply(ledger,"m-final","p3",large)
        state=ledger.export_state()
        self.assertEqual([event["event_id"] for event in state["events"]],["z-first","a-offset","m-final"])
        self.assertEqual(FundingLedger.from_state(state).export_state(),state)
        reordered=deepcopy(state);reordered["events"]=[state["events"][0],state["events"][2],state["events"][1]]
        with self.assertRaises(PerpetualError):FundingLedger.from_state(reordered)

    def test_hostile_decimal_rejected_before_virtual_reads_or_funding_enrollment(self):
        class Hostile(Decimal):
            def is_finite(self):raise AssertionError("virtual finite")
            def as_tuple(self):raise AssertionError("virtual tuple")
        ledger=FundingLedger()
        with self.assertRaises((TypeError,PerpetualError)):
            self.apply(ledger,"hostile","p1",Hostile("1"))
        self.assertEqual(ledger.export_state(),{"schema_version":"funding-ledger@1","events":[],"balances":{}})

    def test_orphan_retained_period_cannot_be_exported_as_partial_state(self):
        ledger=self.initial()
        ledger._events.pop("btc")
        with self.assertRaisesRegex(PerpetualError,"balances do not match events"):
            ledger.export_state()


if __name__=="__main__":unittest.main()
