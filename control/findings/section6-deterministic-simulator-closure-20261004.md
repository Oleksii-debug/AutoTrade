# Section 6 deterministic simulator closure candidate

Initial date: 2026-10-04
Latest closure review: 2026-10-06
Current reconvergence base main: `60e7c95b3b572810dcfb6c4ab34e0b338b0ace02` (#1702)

Current main already contains canonical simulation ownership/recovery, deterministic protocol identity, atomic OMS/finance/settlement convergence, partial fills, and crash-safe ZERO continuation. This candidate closes the remaining demonstrated provider-free simulator determinism/trust gaps.

## Residuals closed

1. Simulation policy chronology
- SIMULATION policy registration uses the frozen simulation timestamp rather than physical process time;
- uninterrupted vs pause/resume durable policy events can remain identical;
- non-SIMULATION scopes cannot consume simulation_time.

2. Execution DTO/scalar authority
- simulator and independent conservative oracle accept exact canonical DTO types;
- caller-owned frozen dataclass instances are detached/reconstructed before authority-bearing use;
- hostile Decimal/string/integer subclasses and post-construction mutation fail closed;
- exact bounded numeric primitives remove ambient Decimal-context authority.

3. MARKET price projection
- MARKET execution uses a versioned price-projection policy bound to InstrumentVersion.price_tick;
- BUY rounds adversely upward and SELL downward to the declared instrument quantum;
- independent oracle reconstructs and verifies the same adverse tick bound;
- mismatched instrument grid/policy fails qualification.

4. Execution qualification evidence
- qualification reads immutable evidence through one authenticated ArtifactStore snapshot and verifies the exact bytes/digest.

## Closure requirements

Section 6 is DONE only after:
1. focused simulation/execution qualification passes on the exact head;
2. full baseline and Verify are terminal green;
3. review state is clean;
4. one integrated provider-free simulation scenario remains deterministic across restart;
5. merge completes;
6. post-merge readback confirms accepted source identity.

No provider/PAPER/LIVE, profitability, economic-edge, signed-release or NVDA qualification is granted.

## Follow-up source review: qualification authority closure

Exact source review after the initial convergence found additional qualification gaps and repaired them on the same canonical Section 6 lineage:

- execution_qualification imported ArtifactStore from a runtime package absent from this exact current-main tree; production and test imports now use the existing canonical autotrade_research.artifacts authority on this lineage;
- execution qualification used non-canonical asset labels (EQUITY/SPOT/MARGIN) instead of the InstrumentVersion asset enum; qualification now uses CASH_EQUITY/FUND/FX/CRYPTO_SPOT/FUTURE/PERPETUAL/OPTION and requires caller/qualification asset identity to equal the canonical instrument;
- qualified order lot size and quantity are bound to InstrumentVersion quantity_step/min/max rules;
- qualified limit/stop and bid/ask/bar prices are bound to InstrumentVersion price_tick/bands;
- qualified order time and market observation time must both lie inside the exact InstrumentVersion effective interval;
- regressions cover false asset-class agreement, sub-step lots, off-grid order/liquidity prices and pre/post-effective instrument use.

These are qualification-integrity repairs only. They do not turn simulation evidence into provider/PAPER/LIVE authority or profitability/edge evidence. Fresh exact-head baseline and Verify are required after these changes.


## Follow-up source review: authoritative instrument metadata cut

The qualified execution path now rejects caller-constructed InstrumentVersion facts unless
their canonical metadata_evidence resolves through the existing authenticated ArtifactStore
reader and is bound to the exact instrument-version facts. Qualified simulation additionally
requires that metadata evidence to have been immutably committed by the order submission cut,
preventing a later-discovered price tick or quantity rule from leaking backward into replay.
Focused regressions cover missing metadata authority and post-order metadata publication.


## 2026-10-06 recovery/send-boundary closure

The candidate was mechanically reconverged onto current main #1702 after the
older full Verify failure was traced exclusively to the independent
research/tests/test_update_producer.py chronology fixtures. The current-main
#1702 repair is inherited rather than duplicated.

Live source review then found a real Section-6 composition gap documented by the
newer isolated simulation lineage #1736: a process death after durable
SubmissionPrepared but before the final send guard could later age beyond the
Prepared owner lease. The simulator already had an atomic zero-wire BLOCKED
terminalizer, but the older current-main dispatcher converted this provably
pre-wire expiry into UNKNOWN and the simulator did not consume recovered
BLOCKED.

The same Section-6 lineage now closes both sides of that boundary:

- an expired SubmissionPrepared with no SubmissionSending evidence becomes
  durable SubmissionBlocked with reason
  prepared_owner_lease_expired_before_send;
- an active Prepared lease remains IN_PROGRESS;
- Sending or later ambiguous states remain UNKNOWN and are never blindly resent;
- the simulator consumes recovered BLOCKED through its existing atomic terminal
  mutation, releasing the reservation and completing the session without
  provider resend or fresh admission;
- the terminal simulation timestamp is inherited from durable SubmissionBlocked
  evidence, so a later restart cannot rewrite causal chronology;
- a concurrent recovery BLOCKED fence prevents a late original final_guard from
  committing SubmissionSending or reaching provider transport;
- no broader cross-attempt WP-18 economic-intent fence is imported here.

Focused regressions:
- mvp/tests/test_simulation_expired_prepared_recovery.py;
- mvp/tests/test_dispatch_prepared_zero_wire_section6.py.

Together with the previously enumerated simulation/execution suites, the
direct Section-6 regression surface now contains at least 239 test methods
across 15 focused files, including 120-episode uninterrupted-vs-restart
equivalence, real os._exit partial-fill recovery, zero-network denial,
build/protocol/config drift, exact execution qualification and conservative
independent-oracle checks.

## Final exact-head rule

This file is durable source evidence, not a self-certifying PASS. The final
accepted SHA is the PR #1617 head created after this documentation update.
Queued, pending, cancelled or older-head CI is not PASS.

Section 6 may be marked DONE only when that final head has:
1. baseline SUCCESS;
2. Verify AutoTrade SUCCESS on both hosted OS jobs;
3. reconvergence-integrity SUCCESS;
4. clean review/thread state;
5. ahead-only topology from the then-current main or a fresh non-loss
   reconvergence if main has advanced;
6. merge to main;
7. post-merge readback proving the accepted Section-6 source identities.

The result remains provider-free simulation authority only. It does not grant
PAPER/LIVE/provider qualification, economic-edge/profitability, signed-release,
native-Windows or human-NVDA qualification.


## 2026-10-06 final Prepared/send race and exact-lease hardening

A final adversarial review of the zero-wire recovery seam found two residuals and
closed both on this canonical Section-6 lineage:

- if recovery had already read durable `SubmissionPrepared` but the original
  final send barrier committed `SubmissionSending` before recovery could commit
  version-2 `SubmissionBlocked`, the recovery CAS conflict could escape as a
  `ValueError`; recovery now reloads durable truth and converges to terminal
  state or the existing `SubmissionSending -> SubmissionUnknown` path instead
  of fabricating zero-wire safety or leaking the version race;
- Prepared lease age no longer passes through
  `timedelta.total_seconds()`/binary float.  It is compared as exact
  `timedelta` chronology, and `prepared_lease_seconds` must be an exact
  built-in positive integer.

Focused regressions now cover both race orders (BLOCKED wins and Sending wins),
two recoveries converging through durable truth, hostile integer lease input,
and the exact microsecond expiry boundary immediately before and at lease
expiration.

These changes preserve the fail-closed rule: before `SubmissionSending`, an
expired Prepared can be proven zero-wire and BLOCKED; once `SubmissionSending`
wins, recovery is UNKNOWN and cannot be blindly resent.
