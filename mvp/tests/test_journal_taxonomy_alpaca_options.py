import unittest

from mvp.autotrade_mvp.alpaca_options import (
    _ALPACA_OPTION_LIFECYCLE_AGGREGATE_TYPE,
)
from mvp.autotrade_mvp.journal_taxonomy import (
    FINANCIAL,
    QUALIFICATION_FINANCIAL,
    require_journal_aggregate_descriptor,
)


class AlpacaOptionLifecycleJournalTaxonomyTests(unittest.TestCase):
    def test_production_alpaca_option_lifecycle_writer_is_qualification_financial(self):
        descriptor = require_journal_aggregate_descriptor(
            _ALPACA_OPTION_LIFECYCLE_AGGREGATE_TYPE
        )

        self.assertEqual(descriptor.domain_classification, FINANCIAL)
        self.assertEqual(
            descriptor.qualification_visibility,
            QUALIFICATION_FINANCIAL,
        )
        self.assertTrue(descriptor.is_financial_for_qualification)


if __name__ == "__main__":
    unittest.main()
