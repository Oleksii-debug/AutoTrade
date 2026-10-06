"""Existing deterministic research baseline over the frozen synthetic dataset.

Causal proposals are diagnostic comparisons. They cannot select a strategy,
change risk, grant permission or establish economic edge.
"""
from datetime import datetime, timedelta
from uuid import uuid5, NAMESPACE_URL
from research.autotrade_research.strategies.deterministic import CausalObservation, ReturnThresholdBaseline
from .persistence import payload_digest


def prepare_research(journal, protocol):
    start = datetime.fromisoformat(protocol['start_time'].replace('Z', '+00:00'))
    baseline = ReturnThresholdBaseline(lookback=3, threshold='0.01', proposal_quantity='1')
    rows = []
    for index, price in enumerate(protocol['prices']):
        instant = start + timedelta(seconds=index)
        observation = CausalObservation.create(event_id=f"synthetic:{protocol['run_id']}:{index+1}",
            symbol='CANONICAL-SIM@1', available_at=instant, price=price)
        baseline.ingest(observation, simulation_time=instant)
        proposal = baseline.propose(symbol=observation.symbol, decision_time=instant)
        rows.append({'episode': index+1, 'action': proposal.action, 'quantity': str(proposal.quantity),
            'reason': proposal.reason, 'input_event_ids': list(proposal.evidence_event_ids),
            'economic_edge_claim': proposal.economic_edge_claim, 'model_calls': proposal.model_calls})
    data = {'protocol_digest': payload_digest(protocol), 'dataset_digest': payload_digest(protocol['prices']),
        'data_origin': 'SYNTHETIC_OWNER_FREE_FIXTURE', 'research_kind': 'ReturnThresholdBaseline',
        'status': 'DIAGNOSTIC_ONLY', 'economic_edge_status': 'INCONCLUSIVE', 'proposals': rows}
    event_id = str(uuid5(NAMESPACE_URL, 'autotrade:product-research:' + data['protocol_digest']))
    event = {'event_id': event_id, 'event_type': 'ProviderFreeResearchCompleted', 'schema_version': '1.0.0',
        'aggregate_type': 'provider_free_research', 'aggregate_id': protocol['run_id'], 'aggregate_version': '1',
        'host_id': 'local-simulation', 'owner_epoch': '1', 'environment': 'SIMULATION',
        'occurred_at': protocol['start_time'], 'observed_at': protocol['start_time'], 'committed_at': protocol['start_time'],
        'correlation_id': event_id, 'causation_id': None, 'payload': data, 'payload_hash': payload_digest(data), 'evidence_refs': []}
    existing = journal.get_event(event_id)
    if existing is None:
        journal.append_event(event)
    elif existing['payload'] != data:
        raise ValueError('frozen research result differs')
    return data
