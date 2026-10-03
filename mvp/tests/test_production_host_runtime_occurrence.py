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

    def test_failed_listener_bootstrap_does_not_publish_runtime_occurrence(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(directory)
            with (
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
                    side_effect=RuntimeError("listener bootstrap failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "listener bootstrap failed"):
                    build_production_host(
                        config,
                        security_boundary=self.DummySecurityBoundary(),
                        principal_resolver=lambda headers, origin: None,
                        snapshot_provider=lambda state, principal: {},
                    )

            journal = production_host.JournalStore(config.journal_path)
            self.assertEqual(
                journal.load_events(
                    "production_host_runtime",
                    config.host_id,
                ),
                [],
            )

    def test_runtime_property_revalidates_after_returned_value_tamper(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(directory)
            runtime = self._build(config)
            try:
                durable_id = runtime.runtime_occurrence.runtime_occurrence_id
                exposed = runtime.runtime_occurrence
                object.__setattr__(
                    exposed,
                    "runtime_occurrence_id",
                    "22222222-2222-4222-8222-222222222222",
                )
                revalidated = runtime.runtime_occurrence
                self.assertEqual(revalidated.runtime_occurrence_id, durable_id)
                self.assertIsNot(revalidated, exposed)
            finally:
                runtime.close()

    def test_forged_cached_occurrence_object_cannot_replace_bound_selector(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(directory)
            runtime = self._build(config)
            try:
                durable = runtime.runtime_occurrence
                object.__setattr__(
                    runtime,
                    "_runtime_occurrence_id",
                    "33333333-3333-4333-8333-333333333333",
                )
                self.assertEqual(
                    runtime.runtime_occurrence.runtime_occurrence_id,
                    durable.runtime_occurrence_id,
                )
            finally:
                runtime.close()

    def test_newer_durable_occurrence_invalidates_bound_runtime_property(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(directory)
            runtime = self._build(config)
            try:
                bound = runtime.runtime_occurrence
                successor = production_host._issue_production_host_runtime_occurrence(
                    runtime.journal,
                    config,
                )
                self.assertNotEqual(
                    successor.runtime_occurrence_id,
                    bound.runtime_occurrence_id,
                )
                with self.assertRaisesRegex(RuntimeError, "latest durable occurrence"):
                    _ = runtime.runtime_occurrence
            finally:
                runtime.close()

    def test_stale_runtime_cannot_adopt_successor_occurrence_by_field_injection(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(directory)
            first = self._build(config)
            first_id = first.runtime_occurrence.runtime_occurrence_id
            first.close()

            successor = self._build(config)
            try:
                successor_id = successor.runtime_occurrence.runtime_occurrence_id
                self.assertNotEqual(first_id, successor_id)
                with self.assertRaisesRegex(RuntimeError, "latest durable occurrence"):
                    _ = first.runtime_occurrence

                object.__setattr__(
                    first,
                    "_runtime_occurrence_id",
                    successor_id,
                )
                with self.assertRaisesRegex(RuntimeError, "latest durable occurrence"):
                    _ = first.runtime_occurrence
            finally:
                successor.close()

    def test_executable_injected_selector_is_not_evaluated(self) -> None:
        with TemporaryDirectory() as directory:
            config = self._config(directory)
            runtime = self._build(config)
            try:
                durable_id = runtime.runtime_occurrence.runtime_occurrence_id

                class ExecutableSelector:
                    invoked = False

                    def __eq__(self, other):
                        self.invoked = True
                        raise AssertionError("injected selector executed")

                    def __ne__(self, other):
                        self.invoked = True
                        raise AssertionError("injected selector executed")

                hostile = ExecutableSelector()
                object.__setattr__(runtime, "_runtime_occurrence_id", hostile)

                self.assertEqual(
                    runtime.runtime_occurrence.runtime_occurrence_id,
                    durable_id,
                )
                self.assertFalse(hostile.invoked)
            finally:
                runtime.close()

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
