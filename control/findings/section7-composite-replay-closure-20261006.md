# Section 7 closure — composite replay and whole-runtime checkpoint

Date: 2026-10-06
Accepted main at closure start: `11da6a62888bb832546a0063ebcbbada0fed7042`

## Result

Section 7 provider-free causal replay/checkpoint semantics are closed on main.

Canonical runtime state requires one verified composite cut containing:
- replay dataset digest, cursor and simulation clock;
- pending deterministic event queue;
- RNG family/state;
- strategy state;
- portfolio/accounting state;
- execution/working/UNKNOWN state;
- accrual/settlement state;
- policy/risk state;
- instrument/version state;
- provider state;
- experiment/protocol state;
- exact JournalStore backing identity;
- source build SHA and protocol identity.

The generic `CompositeReplayCheckpoint` requires all ten canonical runtime-component classes and is verified by the product-selected `RuntimeStateVerifier`. Resume reconstructs the current runtime snapshot, verifies the authority seal, build/protocol bindings and complete fingerprint, and only then exposes another replay event.

## Main-resident evidence

Current main contains decisive regressions including:
- pause/checkpoint/resume matches fresh uninterrupted run;
- terminal same-cut reopen creates no transport or mutation;
- authority-key replacement invalidates resume;
- malformed/missing/tampered checkpoint fails before provider restore or journal mutation;
- foreign durable journal mutation invalidates checkpoint;
- crash after durable completion before checkpoint publication recovers exactly;
- UNKNOWN episode does not mint a new terminal checkpoint;
- observed-fill crash matrix recovers without re-admission or resend;
- durable UNKNOWN rebuild after restart does not permit duplicate exposure;
- caller-constructed or polymorphic runtime authority graphs cannot mint trusted checkpoints.

Required runtime components are enforced in `mvp/autotrade_mvp/replay.py`:
`pending_event_queue`, `rng_state`, `strategy_state`,
`portfolio_accounting_state`, `execution_state`, `accrual_state`,
`policy_state`, `instrument_state`, `provider_state`,
`experiment_state`.

## Boundary

Portable backup/restore of runtime-checkpoint evidence and post-restore authority reconstitution remain WP-49 backup/restore work. They do not create a second Section-7 checkpoint authority and do not reopen semantic resume equivalence.

Hostile-process/sandbox isolation is also a separate qualification boundary; it is not represented here as proof of same-process hostile-code isolation.

No provider/PAPER/LIVE, release, profitability, economic-edge or NVDA qualification follows from this closure.
