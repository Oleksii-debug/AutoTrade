"""Existing deterministic research baseline over the frozen synthetic dataset.

Causal proposals are diagnostic comparisons. They cannot select a strategy,
change risk, grant permission or establish economic edge.
"""
from datetime import datetime, timedelta
from uuid import uuid5, NAMESPACE_URL
from research.autotrade_research.strategies.deterministic import CausalObservation, ReturnThresholdBaseline
from .persistence import payload_digest


def prepare_research(journal, protocol, *, through_episode=None):
    start = datetime.fromisoformat(protocol['start_time'].replace('Z', '+00:00'))
    next_open = 'signal_prices' in protocol
    source = protocol['signal_prices'] if next_open else protocol['prices']
    if next_open:
        if type(through_episode) is not int or not 1 <= through_episode <= len(source):
            raise ValueError('causal research requires a bounded closed-bar prefix')
        source = source[:through_episode]
    elif through_episode is not None:
        raise ValueError('legacy research has no incremental observation contract')
    baseline = ReturnThresholdBaseline(lookback=3, threshold='0.01', proposal_quantity='1')
    rows = []
    for index, price in enumerate(source):
        instant = start + timedelta(seconds=index * protocol.get('observation_interval_seconds', 1))
        observation = CausalObservation.create(event_id=f"synthetic:{protocol['run_id']}:{index+1}",
            symbol='CANONICAL-SIM@1', available_at=instant, price=price)
        baseline.ingest(observation, simulation_time=instant)
        proposal = baseline.propose(symbol=observation.symbol, decision_time=instant)
        rows.append({'episode': index+1, 'action': proposal.action, 'quantity': str(proposal.quantity),
            'reason': proposal.reason, 'input_event_ids': list(proposal.evidence_event_ids),
            'economic_edge_claim': proposal.economic_edge_claim, 'model_calls': proposal.model_calls})
    data = {'protocol_digest': payload_digest(protocol), 'dataset_digest': payload_digest(source),
        'data_origin': ('HISTORICAL_BAR_CLOSE_SCENARIO' if next_open
                        else 'SYNTHETIC_OWNER_FREE_FIXTURE'),
        'research_kind': 'ReturnThresholdBaseline',
        'status': 'DIAGNOSTIC_ONLY', 'economic_edge_status': 'INCONCLUSIVE', 'proposals': rows}
    if next_open:
        data['through_episode'] = through_episode
        # The journal already has one immutable event per observed bar.  Do not
        # copy the entire causal prefix into every later row: that makes both
        # storage and authenticated checkpoint scans quadratic.  Retain the
        # new proposal plus a digest of the exact prefix used to produce it.
        # The full prefix remains available to the in-process specialist below
        # and every individual proposal remains durable in its own event.
        durable_data = {
            key: value for key, value in data.items() if key != 'proposals'
        }
        durable_data['proposal'] = rows[-1]
        durable_data['proposal_prefix_digest'] = payload_digest(rows)
    else:
        durable_data = data
    event_id = str(uuid5(NAMESPACE_URL, 'autotrade:product-research:' +
                         data['protocol_digest'] + (f':{through_episode}' if next_open else '')))
    event = {'event_id': event_id, 'event_type': 'ProviderFreeResearchCompleted', 'schema_version': '1.0.0',
        'aggregate_type': 'provider_free_research', 'aggregate_id': protocol['run_id'],
        'aggregate_version': str(through_episode) if next_open else '1',
        'host_id': 'local-simulation', 'owner_epoch': '1', 'environment': 'SIMULATION',
        'occurred_at': protocol['start_time'], 'observed_at': protocol['start_time'], 'committed_at': protocol['start_time'],
        'correlation_id': event_id, 'causation_id': None, 'payload': durable_data,
        'payload_hash': payload_digest(durable_data), 'evidence_refs': []}
    existing = journal.get_event(event_id)
    if existing is None:
        journal.append_event(event)
    elif existing['payload'] != durable_data:
        raise ValueError('frozen research result differs')
    return data
