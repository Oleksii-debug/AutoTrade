"""Synthetic immediate settlement profile using the canonical settlement authority.

These receipts describe the internal simulator, never an exchange or a real
settlement rule. All evidence is confined to the canonical SIMULATION scope.
"""
from dataclasses import replace
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5
from .persistence import canonical_json
from .durable_settlement import (DurableSettlementBook, SETTLEMENT_EVIDENCE_MEDIA_TYPE,
    settlement_rule_evidence_receipt, settlement_rule_evidence_metadata,
    settlement_completion_evidence_receipt, settlement_completion_evidence_metadata)
from .settlement import (SettlementAccountScope, SettlementRuleBinding, SettlementEvidence,
                         equity_cash_obligation_from_transaction)


def settle_simulated_transactions(store, economic, artifacts, *, transaction_ids, timestamp):
    from .simulation_session import ACCOUNT, PROVIDER, ENVIRONMENT, INSTRUMENT
    instant = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    day = instant.date()
    scope = SettlementAccountScope(PROVIDER, ACCOUNT, ENVIRONMENT)
    raw_rule = SettlementRuleBinding(rule_id='internal-simulator-t0', rule_version='1', scope=scope,
        instrument_version=INSTRUMENT, settlement_currency='USD', effective_from=day,
        effective_to=None, evidence_refs=('simulation:synthetic-immediate-settlement-v1',))

    def publish(receipt, metadata, label):
        raw = canonical_json(receipt).encode()
        artifact_id = str(uuid5(NAMESPACE_URL, 'autotrade:simulation-settlement:' + label + ':' + canonical_json(receipt)))
        manifest = artifacts.publish_bytes(artifact_id=artifact_id, data=raw,
            media_type=SETTLEMENT_EVIDENCE_MEDIA_TYPE, rights={'storage': True, 'export': False},
            source_refs=['simulation:synthetic-immediate-settlement-v1'], metadata=metadata)
        return f"artifact:{artifact_id}@{manifest['sha256']}"

    rule_ref = publish(settlement_rule_evidence_receipt(raw_rule, trade_date=day, expected_settlement_date=day),
        settlement_rule_evidence_metadata(raw_rule, trade_date=day, expected_settlement_date=day), 'rule')
    rule = replace(raw_rule, evidence_refs=(*raw_rule.evidence_refs, rule_ref))
    book = DurableSettlementBook(store, provider_id=PROVIDER, account_id=ACCOUNT, environment=ENVIRONMENT,
        evidence_artifact_root=artifacts.root, evidence_artifact_store=artifacts)
    transactions = {t.transaction_id: t for t in economic.transactions}
    for tid in transaction_ids:
        obligation = equity_cash_obligation_from_transaction(transactions[tid],
            obligation_id='simulation-settlement:' + tid, instrument=INSTRUMENT,
            settlement_currency='USD', settlement_date=day, rule_binding=rule)
        register_id = str(uuid5(NAMESPACE_URL, 'autotrade:settlement-register:' + tid))
        book.register_obligations((obligation,), command_id=register_id,
            idempotency_key=register_id, committed_at=timestamp)
        raw_evidence = SettlementEvidence(obligation.obligation_id, 'simulation:completed:' + tid, instant)
        evidence_ref = publish(settlement_completion_evidence_receipt(scope=scope, obligation=obligation, evidence=raw_evidence),
            settlement_completion_evidence_metadata(scope=scope, obligation=obligation, evidence=raw_evidence), 'completion')
        complete_id = str(uuid5(NAMESPACE_URL, 'autotrade:settlement-complete:' + tid))
        book.apply_settlement(replace(raw_evidence, evidence_ref=evidence_ref), as_of=day,
            command_id=complete_id, idempotency_key=complete_id, committed_at=timestamp)
    projection = book.project(economic)
    if projection.available_to_spend('USD') != economic.cash('USD'):
        raise ValueError('simulated settlement disagrees with canonical cash')
    return book
