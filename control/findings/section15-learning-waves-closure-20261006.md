# Section 15 closure — causal learning waves and isolated candidate validation

Date: 2026-10-06

## Canonical result

Section 15 source/integration authority is closed on `main`.

- Final PR: #1630
- Accepted head: `5421dba999024cc56e9b1c2411cb9060ad5ca898`
- Exact base before merge: `69e74703fa2ece6127bc889525c4024f73427c69`
- Merge commit: `929f47046d7103ef2292ae23480c567c447bf66d`
- Candidate tree: `f41b732d79b668fed75ef6b65a546fc804da524e`
- Post-merge main tree at the closure cut: `f41b732d79b668fed75ef6b65a546fc804da524e`
- Post-merge tree equality: PASS

## Learning-wave authority closed

The accepted coordinator establishes a deterministic, provider-free learning-wave boundary:

- pause triggers are explicit versioned policy inputs; there is no hidden universal trade/day threshold;
- wave state is bound to champion artifact identity and one paused causal source cut;
- error analysis must occur after the pause and before candidate creation;
- candidate construction is bound to one exact frozen protocol;
- training population is complete and bound to the paused source cut;
- validation population must be independently identified, use a distinct causal cut, be disjoint from training observations and cannot be opened before the candidate is fixed;
- canonical approval must bind exact candidate id, artifact hash and frozen protocol;
- expired, FAIL, retention-failed or risk-failed evidence rejects the handoff;
- INCONCLUSIVE continues validation rather than inventing PASS;
- AUTO mode only hands off to the existing canonical promotion authority;
- every Section-15 resolution explicitly records `grants_trading_authority=false`;
- immutable resolution evidence is retained in the canonical ArtifactStore.

## Executable-caller / mutation hardening

The final head additionally closes the authority seams found during the current-main audit:

- exact built-in decimal, integer, string, tuple and rights-container ingress;
- exact `LearningWavePolicy`, `MarketWaveSnapshot`, `PauseDecision`, `CandidateWave`, `CandidateApproval` and `PopulationCoverageManifest` types at authority-bearing boundaries;
- policy, market snapshot, pause, population manifests, candidate wave and approval are reconstructed/detached and revalidated before use;
- mutation of caller-owned population evidence after candidate creation cannot rewrite the candidate's frozen population cut;
- post-construction mutation of wave or approval state is revalidated and fails closed;
- resolution publication requires the exact canonical `ArtifactStore`;
- publication uses class-qualified `ArtifactStore.publish_bytes`, preventing an instance/subclass override from becoming evidence authority;
- publication rights are snapshotted from exact inert built-in values before the store is touched.

The focused Section-15 regression file contains 54 tests after hardening, including hostile Decimal, policy/snapshot/population/approval subclass, post-construction mutation, ArtifactStore override and hostile rights-text falsifiers.

## Promotion boundary

Section 15 does not replace or weaken the existing promotion authority.

`CandidateApproval` is a handoff object only. The existing `ChampionRegistry` / scientific registry remains responsible for terminal promotion/routing verification. Section-15 resolution itself never grants trading authority and does not mutate risk or provider execution.

## Qualification boundary

Fresh exact-head baseline, research-primitives, science-qualification, provider-free-product and full Verify jobs were registered for the accepted source head but remained queued without runner assignment. Queued is not represented as PASS.

This closes the Section-15 causal learning-wave coordinator and isolated candidate-validation source/integration boundary. It does not mark all of WP-38 or WP-42 complete. Actual bounded online-update generation, drift calibration, broader continual-learning qualification, production champion routing, provider/PAPER/LIVE authority, economic-edge/profitability evidence, signed release and physical Windows/NVDA qualification remain separately owned gates.
