from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import (
    RiskAuthorityRequest, AuthoritativeRiskSnapshot, AuthorityService, AuthorityConflict,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.risk import RiskIntent, RiskContext
from mvp.autotrade_mvp.risk_policy_authority import DurableRiskPolicyRegistry, RiskPolicyScope, RiskPolicyAuthorityError
from mvp.autotrade_mvp.simulation_session import _risk_policy

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
TIME = "2026-10-03T00:00:00Z"
SCOPE = RiskPolicyScope("SIMULATED", "account", "SIMULATION", "SIMULATION", "internal", "EQUITY")


def issue(store, policy_id="quantitative", version=1, policy=None):
    registry = DurableRiskPolicyRegistry(store)
    registry.register(scope=SCOPE, policy_id=policy_id, version=version, policy=policy or _risk_policy(), committed_at=NOW)
    registry.activate(scope=SCOPE, policy_id=policy_id, version=version, committed_at=NOW)
    return registry.resolve_current(SCOPE)


def request(resolved):
    return RiskAuthorityRequest(risk_intent=RiskIntent.create(symbol="A", side="BUY", quantity="1", price="100", expected_state_version=1),
        account_id="account", environment="SIMULATION", provider_id="SIMULATED",
        instrument_version=("11111111-1111-4111-8111-111111111111", 1), capability_snapshot_id="sim-cap",
        reconciliation_checkpoint_event_id="reconciled", journal_sequence_cut=resolved.resolved_journal_sequence_cut,
        reservation_version=0, reservation_state_digest="state", authority_policy_id="authority", authority_policy_version=1,
        evaluated_at=TIME, resolved_risk_policy=resolved, provider_environment="SIMULATION", entity_policy_id="internal", instrument_family="EQUITY")


def snapshot(req, **updates):
    values = {key: value for key, value in vars(req).items() if key != "risk_intent"}
    values.update(context=RiskContext.create(state_version=1, equity="1000", positions={}, marks={"A": "100"},
        margin_headroom="1", capability_allowed=True, borrow_available=True), risk_policy=req.resolved_risk_policy.policy,
        valid_until="2026-10-03T00:01:00Z", evidence_refs={"PORTFOLIO": "book", "MARKET": "mark", "MARGIN": "margin",
            "POLICY": req.resolved_risk_policy.registration_event_id, "RECONCILIATION": "reconciled", "CAPABILITY": "sim-cap", "BORROW": "borrow"})
    values.update(updates)
    return AuthoritativeRiskSnapshot(**values)


class ResolvedPolicyFinancialCompositionTests(unittest.TestCase):
    def test_registry_issued_policy_is_cross_bound_by_service_and_snapshot(self):
        with TemporaryDirectory() as d:
            store = JournalStore(Path(d) / "journal.sqlite3")
            resolved = issue(store)
            req = request(resolved)
            service = AuthorityService(store, risk_policy_scope=SCOPE, risk_authority_resolver=snapshot)
            accepted = service._resolve_authoritative_risk_snapshot(req)
            self.assertEqual(accepted.evidence_payload()["resolved_risk_policy"], resolved.evidence_payload)

    def test_cross_store_issued_policy_is_not_same_financial_authority(self):
        with TemporaryDirectory() as d:
            first, second = JournalStore(Path(d) / "a.sqlite3"), JournalStore(Path(d) / "b.sqlite3")
            issue(first)
            foreign = issue(second)
            service = AuthorityService(first, risk_policy_scope=SCOPE, risk_authority_resolver=snapshot)
            with self.assertRaisesRegex(AuthorityConflict, "service-selected journal"):
                service._resolve_authoritative_risk_snapshot(request(foreign))

    def test_scope_and_cut_substitution_fail_before_resolver(self):
        with TemporaryDirectory() as d:
            resolved = issue(JournalStore(Path(d) / "j.sqlite3"))
            original = request(resolved)
            for field, value in (("provider_environment", "DEMO"), ("entity_policy_id", "other"),
                                 ("instrument_family", "OPTION"), ("journal_sequence_cut", original.journal_sequence_cut + 1)):
                with self.subTest(field=field), self.assertRaises(AuthorityConflict):
                    replace(original, **{field: value})

    def test_looser_caller_policy_cannot_replace_registered_content(self):
        from decimal import Decimal
        with TemporaryDirectory() as d:
            resolved = issue(JournalStore(Path(d) / "j.sqlite3"))
            with self.assertRaisesRegex(AuthorityConflict, "content differs"):
                snapshot(request(resolved), risk_policy=replace(resolved.policy, max_single_notional=Decimal("999999")))

    def test_policy_evidence_cannot_be_free_caller_label(self):
        with TemporaryDirectory() as d:
            resolved = issue(JournalStore(Path(d) / "j.sqlite3"))
            normal = snapshot(request(resolved))
            refs = dict(normal.evidence_refs)
            refs["POLICY"] = "caller:policy"
            with self.assertRaisesRegex(AuthorityConflict, "POLICY evidence"):
                replace(normal, evidence_refs=refs)

    def test_raw_policy_mutation_is_rejected_again_at_snapshot_use(self):
        from decimal import Decimal
        with TemporaryDirectory() as d:
            resolved = issue(JournalStore(Path(d) / "j.sqlite3"))
            normal = snapshot(request(resolved))
            object.__setattr__(resolved.policy, "max_abs_position", Decimal("999999"))
            with self.assertRaises(RiskPolicyAuthorityError):
                normal.evidence_payload()

    def test_activation_episode_is_part_of_snapshot_identity(self):
        with TemporaryDirectory() as d:
            store = JournalStore(Path(d) / "j.sqlite3")
            one = snapshot(request(issue(store)))
            two = snapshot(request(issue(store, policy_id="same-values-other-policy")))
            self.assertNotEqual(one.snapshot_id, two.snapshot_id)

    def test_scope_is_detached_from_caller_mutation_at_service_initialization(self):
        with TemporaryDirectory() as d:
            store = JournalStore(Path(d) / "j.sqlite3")
            resolved = issue(store)
            supplied = RiskPolicyScope(*SCOPE.payload().values())
            service = AuthorityService(store, risk_policy_scope=supplied, risk_authority_resolver=snapshot)
            object.__setattr__(supplied, "entity_policy_id", "looser")
            self.assertEqual(service._resolve_authoritative_risk_snapshot(request(resolved)).resolved_risk_policy.identity.scope, SCOPE)


if __name__ == "__main__":
    unittest.main()
