# Worker D: provider-free financial and autonomous convergence

Status: integration candidate, sections 13/14/15/16/19 ADVANCED; none CLOSED.
Base refreshed from live main `cc3599daa3d7d42a4f55cca3103cc89d52689a8b`.
Exact source head and terminal verification results belong in the associated PR;
this record makes no claim from queued CI or from tests on an earlier source.

## Canonical lineage, not replacement engines

The candidate carries path-scoped semantic results from existing owners:

| Owner | Source head used | Consumed result |
|---|---|---|
| #1263 | 4839e88114eebfae73fec688139c8c6a021d2687 | registry-issued quantitative RiskPolicy sealing |
| #1269 | da38f36c5c7a112b9d56a6848b7838b08209d65a | exact-rational hard-risk verdicts and liquidation reads |
| #1291 | c76ba92782e37ec64ecee6956f2b6691d258aad6 | AuthorityService physical JournalStore-generation sealing |
| #1272 | a2ff7c3c40fbc65ff865f73fff414718bb7e0c98 | durable valuation and policy-bound freshness |
| #1298 | 8863f182ce9a78c9127ff432ad8052d3dc7f358e | composition contract; predecessor contained structural tests, not implementation |
| #1300 | 35ac02a7351061522e9e30ebffb6ab480bafefab | immutable financial request content material, no send authority |
| #1302 | 66a7420b0fa3f4b36bd29dc54ef92d898fa31792 | canonical exact accounting/reservations/reconciliation financial packet |
| #1303 | 6190394381c80a2eeed53cbb31c812d03d791105 | immutable provider financial scope |
| #1325 | dc87a55b62aefbe4f78da75300c1326c1ed3d4e7 | shared provider-environment normalizer |
| #1318 | 5f72e8bbbc121b8626f9e0a541cac74d09a9a756 | trusted scientific evidence graph and semantic-owner interlock |
| #1249 | 749a6a9e1bbdadd6b23e74f06f1a0b394b92ef07 | Python socket/DNS-denial design, promoted to the ZERO loop |

No stale ancestry or old ablation files are replayed. Main's merged #1279 exact
ablation authority is retained. #1134 remains a separate research strategy
producer lineage; the executable loop uses the already-canonical
MovingAverageStrategy. No second risk engine, OMS, allocator, agent strategy,
accounting ledger or science gate is introduced.

## Executable product path

`run_autonomous_simulation` extends the existing `simulation_session.py`.
It drives each observation of one frozen stream without operator/model input:
canonical economic state and simulated account reconciliation -> versioned
InstrumentRegistry and durable valuation -> existing deterministic strategy ->
existing allocator -> exact hard risk -> AuthorityService admission and durable
reservation -> GuardedDispatcher and durable canonical OMS -> deterministic
SimulatedProvider fill evidence -> canonical financial posting and reservation
consumption -> reconciliation -> durable portfolio checkpoint -> next decision.

The simulator's restart image is carried in the canonical JournalStore, never
in a competing ledger. Restore checks simulator cash/positions against the
canonical economic book. Protocol identity freezes observation population,
chronology, instrument definition, strategy, risk-policy identifier, fees and
fault/emergency schedule. Completed prefixes can be resumed. An unfinished
episode blocks continuation and reports UNKNOWN with zero new sends.

Repeated BUY signals propose a target of one share; they do not repeatedly add
one share. SELL signals flatten owned long inventory through REDUCE and
reduce-only hard risk. No short inventory or unfilled sale proceeds are used.
Cash reservations include BUY principal+fee or the REDUCE cash fee. The loop
conservatively blocks all new decisions with unresolved OMS or positive/UNKNOWN
reservation obligations. This is a sequential internal account, not a general
concurrent multi-instrument inventory reservation claim.

The admitted RiskAuthorityRequest and AuthoritativeRiskSnapshot retain the
same registry-issued policy, exact provider/account/domain/family scope,
registration+activation chronology, policy-content identity and journal cut.
Service configuration is detached into its existing closure-owned store
binding. The resolver revalidates issuance and owning store at use time; final
composition must return exactly the original snapshot. Caller policy content
and free POLICY evidence labels cannot replace registered authority.

Allocation admission and securities-borrow subtraction now use shared exact
arithmetic. OMS filled/open/overfilled quantities and correction/bust verdicts
also use the shared exact authority. Average fill price is a reporting-only
projection: terminating averages stay exact; non-terminating averages use an
explicit 1e-18 HALF_EVEN quantum. Individual fill prices/quantities remain exact.

ZERO execution does not invoke a model. The synchronous loop fences Python
socket connect/send/UDP and DNS entry points, including suppressed attempts.
This is not an OS/native-extension/process network sandbox.

## Operator entrypoint

```
PYTHONPATH=.:research python -m mvp.autotrade_mvp.cli \
  --autonomous-simulation --state-dir internal-run \
  --episode-id scenario-1 --at 2026-10-03T00:00:00Z \
  --prices 100,101,103,102,100,100,101,103
```

`--stop-after-episodes N` pauses an exact frozen stream. Repeating the same command
without that option resumes the remaining prefix; changing input/time/faults
fails. `--fault-episode 3` exercises sticky UNKNOWN. `--emergency-episode 4`
continues observations with NO_TRADE. UNKNOWN exits with code 2.

Existing `--status`, `--accessible-status`, `--economic-report` and `--history`
read the autonomous journal without send or financial mutation. They revalidate
completed simulator/economic/OMS/reconciliation cuts and report simulation-only
P&L. No economic edge is inferred from that P&L.

## Material acceptance and honest limits

Adversarial coverage includes 120 unattended observations, deterministic
continuous-vs-pause/restart equivalence, duplicate exposure avoidance, reduction
and repeated signals, rejected-risk continuation, emergency continuation,
acknowledgement without fill, crash during SENDING, response loss, unchanged
journal on UNKNOWN retry, model non-invocation, network/DNS/UDP denial,
conflicting economics, pending OMS obligations, exact partial/cancel/fill/bust
quantities, policy-scope/cut/store substitution and post-issuance mutation.

Scientific graph verification retains one authenticated artifact snapshot,
independently selected storage root, exact source and graph identity, and signed
independent-review verification. Locally produced favorable evidence cannot
self-issue terminal PASS. The semantic-owner gate deliberately remains
INCONCLUSIVE until trial/holdout/causal/after-cost facts are reconstructed from
canonical owners. Future or self-authored favorable claims cannot use this
candidate to issue earlier frozen scientific PASS.

Full-repository integration exposed stale fixtures that lacked execution direction
and provenance, and fault injection that shadowed sealed owner instances. Fixtures
now carry explicit synthetic evidence; faults patch class seams without relaxing
production authority. Borrow mismatch/recall checkpoints remain incomplete and
cannot admit cash-only trades through that conflicted financial cut. The existing
atomic cash-replay facade now consumes the canonical exact store-generation scope
and detached activity validator rather than bypassing its retained implementation.
A targeted 221-test integration packet passed after these repairs.

Remaining provider-free blockers preventing section closure:

1. OMS fill projection and economic/reservation consumption are separate commits;
   a crash between them stays UNKNOWN and requires explicit internal recovery.
2. The loop is sequential, long-only, one synthetic registered cash-equity
   instrument; general settled/unsettled capital, multi-instrument allocation,
   inventory reservations and exact evidence-bound allocator admission still
   require canonical owning projections. No settlement authority is invented.
3. #1300 binding material is retained but is not a product-owned authority issuer;
   general OMS replace/correction/bust-to-economics atomic composition is not
   established by this bounded autonomous orchestration.
4. Trusted scientific semantic-owner population/protocol/holdout/multiplicity/
   leakage/after-cost/frozen-time composition and independent attestation remain
   missing. #1318 cannot honestly issue terminal scientific PASS yet.
5. Python socket denial does not establish native/process network denial.
6. Exact-head remote dual-OS CI, .NET/Windows/NVDA and general release acceptance
   are separate evidence; pending or unavailable checks are never PASS.

Real providers, credentials, PAPER/LIVE execution, real-money readiness and
profitability/economic-edge claims are outside this candidate.
