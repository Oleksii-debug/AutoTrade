from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.durable_capabilities import DurableCapabilityRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_route_dispatch import (
    ProviderRouteDispatchError,
    compose_selected_provider_route_authority,
)
from mvp.tests.test_provider_route_dispatch import (
    ProviderRouteDispatchTests,
    successor_spot_q,
)
from mvp.tests.test_provider_selection import NOW


_AT = NOW.isoformat().replace("+00:00", "Z")


class SelectedRouteAuthorityCompositionTests(unittest.TestCase):
    def _fixture(self, directory: str):
        fixture = ProviderRouteDispatchTests(
            methodName="test_success_binds_q_and_c_into_durable_submission_scope"
        )
        self.addCleanup(fixture.doCleanups)
        return fixture.setup_route(directory)

    @staticmethod
    def _compose(journal, capabilities, qualifications, route, authority_check):
        return compose_selected_provider_route_authority(
            store=journal,
            environment="PAPER",
            account_id="paper-account",
            route=route,
            capability_registry=capabilities,
            qualification_registry=qualifications,
            authority_check=authority_check,
        )

    def test_exact_current_c_q_extend_upstream_financial_guard(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            combined = self._compose(
                journal,
                capabilities,
                qualifications,
                route,
                lambda intent_hash, at: (True, "financial_authority_current"),
            )
            self.assertEqual(
                combined("intent-hash", _AT),
                (True, "financial_authority_current"),
            )

    def test_upstream_financial_rejection_is_not_upgraded_by_current_c_q(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            combined = self._compose(
                journal,
                capabilities,
                qualifications,
                route,
                lambda intent_hash, at: (False, "financial_risk_rejected"),
            )
            self.assertEqual(
                combined("intent-hash", _AT),
                (False, "financial_risk_rejected"),
            )

    def test_q2_cannot_upgrade_request_bound_to_q1(self):
        with TemporaryDirectory() as directory:
            journal, capabilities, qualifications, route, _dispatcher, q1, harness = (
                self._fixture(directory)
            )
            combined = self._compose(
                journal,
                capabilities,
                qualifications,
                route,
                lambda intent_hash, at: (True, "financial_authority_current"),
            )
            q2, receipt2, protocol2 = successor_spot_q(
                old_qualification_id=q1.qualification_id
            )
            harness.register(
                protocol_key=protocol2.key,
                record=q2,
                receipt=receipt2,
            )
            qualifications._append_accepted(
                protocol_key=protocol2.key,
                record=q2,
                receipt=receipt2,
            )
            self.assertEqual(
                combined("intent-hash", _AT),
                (False, "provider_qualification_not_exact_current"),
            )

    def test_c_q_registries_must_share_exact_financial_journal(self):
        with TemporaryDirectory() as directory:
            journal, _capabilities, qualifications, route, _dispatcher, _q1, _harness = (
                self._fixture(directory)
            )
            other = JournalStore(Path(directory) / "other.sqlite3")
            capabilities = DurableCapabilityRegistry(other)
            with self.assertRaisesRegex(
                ProviderRouteDispatchError,
                "share one JournalStore instance",
            ):
                self._compose(
                    journal,
                    capabilities,
                    qualifications,
                    route,
                    lambda intent_hash, at: (True, "financial_authority_current"),
                )


if __name__ == "__main__":
    unittest.main()
