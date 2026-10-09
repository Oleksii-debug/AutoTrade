"""Provider-free deterministic specialist using the existing causal agent DAG.

An agent proposal cannot grant authority. Allocation, hard risk, reservation,
guarded dispatch and canonical financial bookkeeping still decide execution.
"""
from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5
from research.autotrade_research.agents.dag import (
    SpecialistSpec, SpecialistRun, plan_specialists, aggregate_specialists,
)
from .persistence import payload_digest
from .product_research import prepare_research


def decide(journal, protocol, *, episode, strategy_side, position, timestamp):
    research = prepare_research(journal, protocol)
    # Every admitted input was available at this observation. Later research
    # rows in the retained offline dataset never enter the specialist snapshot.
    input_digest = payload_digest({'protocol_digest': payload_digest(protocol),
        'episode': episode, 'prices': protocol['prices'][:episode],
        'research': research['proposals'][:episode], 'position': str(position),
        'strategy_side': strategy_side})
    specs = (SpecialistSpec('deterministic-trend', 'one-strategy', Decimal('0'), Decimal('1'),
        required_inputs=('market-prefix', 'causal-research-prefix')),)
    inputs = ('market-prefix', 'causal-research-prefix')
    plan = plan_specialists(specs, input_snapshot_id=input_digest,
        available_inputs=inputs, total_budget='0')
    score = Decimal('1') if strategy_side == 'BUY' else Decimal('-1') if strategy_side == 'SELL' else Decimal('0')
    point = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    run = SpecialistRun('deterministic-trend', input_digest,
        'LONG' if score > 0 else 'SHORT' if score < 0 else 'FLAT', score, Decimal('1'),
        ('synthetic-causal-input:' + input_digest,), Decimal('0'), point)
    result = aggregate_specialists(specs, (run,), plan=plan, input_snapshot_id=input_digest,
        available_inputs=inputs, total_budget='0', decision_deadline=point)
    side = 'BUY' if result.direction == 'LONG' else 'SELL' if result.direction == 'SHORT' else 'HOLD'
    data = {'episode': episode, 'input_snapshot_id': input_digest,
        'protocol_digest': payload_digest(protocol), 'strategy_side': strategy_side,
        'agent_side': side, 'accepted_roles': list(result.accepted_roles),
        'evidence_refs': list(result.evidence_refs), 'total_cost': str(result.total_cost),
        'model_calls': 0, 'live_authority_granted': result.live_authority_granted,
        'sell_semantics': 'REDUCE_OWNED_LONG_ONLY', 'economic_edge_status': 'INCONCLUSIVE'}
    event_id = str(uuid5(NAMESPACE_URL, 'autotrade:zero-agent:' + input_digest))
    event = {'event_id': event_id, 'event_type': 'SimulationAgentDecisionRecorded',
        'schema_version': '1.0.0', 'aggregate_type': 'simulation_agent_decision',
        'aggregate_id': protocol['run_id'], 'aggregate_version': str(episode),
        'host_id': 'local-simulation', 'owner_epoch': '1', 'environment': 'SIMULATION',
        'occurred_at': timestamp, 'observed_at': timestamp, 'committed_at': timestamp,
        'correlation_id': event_id, 'causation_id': None, 'payload': data,
        # Specialist DAG references are advisory string labels.  The canonical
        # JournalStore envelope accepts only EvidenceRef objects, so retain the
        # labels in the diagnostic payload and do not misrepresent them as
        # verified artifact references.
        'payload_hash': payload_digest(data), 'evidence_refs': []}
    existing = journal.get_event(event_id)
    if existing is None:
        journal.append_event(event)
    elif existing['payload'] != data:
        raise ValueError('retained deterministic agent decision differs')
    return side
