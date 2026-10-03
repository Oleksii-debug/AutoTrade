from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from mvp.autotrade_mvp import host_network, production_host
from mvp.autotrade_mvp.journal_taxonomy import (
    NON_FINANCIAL,
    QUALIFICATION_NON_FINANCIAL,
    require_journal_aggregate_descriptor,
)
from mvp.autotrade_mvp.production_host import (
    ProductionHostConfig,
    build_production_host,
    require_current_production_host_runtime_occurrence,
)


class ProductionHostRuntimeOccurrenceContractTests(unittest.TestCase):
    class DummySecurityBoundary:
        pass

    @staticmethod
    def _config(directory: str) -> ProductionHostConfig:
        return ProductionHostConfig(
            journal_path=Path(directory).resolve() / "journal.sqlite3",
            account_id="paper-account",
            environment="PAPER",
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=8765,
            public_origin="http://127.0.0.1:8765",
        )

    def _build(self, config: ProductionHostConfig):
        patches = (
            patch.object(
                production_host,
                "SecurityBoundary",
                self.DummySecurityBoundary,
            ),
            patch.object(
                host_network,
                "SecurityBoundary",
                self.DummySecurityBoundary,
            ),
            patch.object(
                production_host,
                "AuthenticatedHostServer",
                return_value=Mock(),
            ),
        )
        with patches[0], patches[1], patches[2]:
            return build_production_host(
                config,
                security_boundary=self.DummySecurityBoundary(),
                principal_resolver=lambda headers, origin: None,
                snapshot_provider=lambda state, principal: {},
            )

    def test_runtime_occurrence_is_durable_unique_and_restart_ordered(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(directory)
            first = self._build(config)
            try:
                first_events = first.journal.load_events(
                    "production_host_runtime",
                    config.host_id,
                )
                self.assertEqual(len(first_events), 1)
                first_event = first_events[0]
                self.assertEqual(
                    first_event["event_type"],
                    "ProductionHostRuntimeOccurrenceIssued",
                )
                self.assertEqual(first_event["aggregate_version"], 1)
                self.assertGreater(first_event["journal_sequence"], 0)
                self.assertEqual(
                    set(first_event["payload"]),
                    {
                        "account_id",
                        "environment",
                        "host_id",
                        "runtime_occurrence_id",
                        "schema_version",
                    },
                )
                self.assertEqual(first_event["payload"]["schema_version"], "1.0.0")
                self.assertEqual(first_event["payload"]["host_id"], config.host_id)
                self.assertEqual(
                    first_event["payload"]["account_id"],
                    config.account_id,
                )
                self.assertEqual(
                    first_event["payload"]["environment"],
                    config.environment,
                )
                first_occurrence = first_event["payload"]["runtime_occurrence_id"]
                self.assertEqual(str(UUID(first_occurrence)), first_occurrence)
                first_occurrence_value = first.runtime_occurrence
                self.assertEqual(
                    require_current_production_host_runtime_occurrence(
                        journal=first.journal,
                        occurrence=first_occurrence_value,
                    ),
                    first_occurrence_value,
                )
            finally:
                first.close()

            successor = self._build(config)
            try:
                events = successor.journal.load_events(
                    "production_host_runtime",
                    config.host_id,
                )
                self.assertEqual(len(events), 2)
                self.assertEqual(
                    [event["aggregate_version"] for event in events],
                    [1, 2],
                )
                self.assertLess(
                    events[0]["journal_sequence"],
                    events[1]["journal_sequence"],
                )
                second_occurrence = events[1]["payload"]["runtime_occurrence_id"]
                self.assertEqual(str(UUID(second_occurrence)), second_occurrence)
                self.assertNotEqual(first_occurrence, second_occurrence)
                with self.assertRaisesRegex(PermissionError, "no longer current"):
                    require_current_production_host_runtime_occurrence(
                        journal=successor.journal,
                        occurrence=first_occurrence_value,
                    )
                self.assertEqual(
                    require_current_production_host_runtime_occurrence(
                        journal=successor.journal,
                        occurrence=successor.runtime_occurrence,
                    ),
                    successor.runtime_occurrence,
                )
            finally:
                successor.close()

    def test_runtime_occurrence_family_is_nonfinancial_for_qualification(self) -> None:
        descriptor = require_journal_aggregate_descriptor(
            "production_host_runtime"
        )
        self.assertEqual(descriptor.domain_classification, NON_FINANCIAL)
        self.assertEqual(
            descriptor.qualification_visibility,
            QUALIFICATION_NON_FINANCIAL,
        )


if __name__ == "__main__":
    unittest.main()
