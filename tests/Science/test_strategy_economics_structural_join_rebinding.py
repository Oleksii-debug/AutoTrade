from __future__ import annotations

import unittest

import qualification.strategy_economics.qualify as economics_authority
from qualification.strategy_economics.qualify import assess_strategy_economics_authority
from tests.Science.test_strategy_economics_authority import _binding, _proposal, _registry


class StrategyEconomicsStructuralJoinRebindingTests(unittest.TestCase):
    def test_public_structural_join_rebind_is_never_executed(self) -> None:
        item = _proposal()
        calls = []
        original = economics_authority.bind_strategy_economics

        def hostile_join(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("rebound public structural economics join executed")

        economics_authority.bind_strategy_economics = hostile_join
        try:
            assessment = assess_strategy_economics_authority(
                item,
                _binding(item),
                instrument_registry=_registry(),
            )
        finally:
            economics_authority.bind_strategy_economics = original

        self.assertEqual(calls, [])
        self.assertEqual(assessment.status, "INCONCLUSIVE")
        self.assertIn("structural_economics_binding", assessment.verified_owners)
        self.assertIn("instrument_registry_authority", assessment.unresolved_owners)


if __name__ == "__main__":
    unittest.main()
