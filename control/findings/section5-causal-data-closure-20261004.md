# Section 5 causal data closure candidate

Date: 2026-10-04
Parent: Section 1 contract-v6 current-main closure candidate
Parent head: `fc9aad050f4e980d5e89aca55dc1af3f1726f566`

Current main already contains the sealed InstrumentRegistry, causal instrument lookup and the previously qualified raw market-evidence chronology. This candidate closes the remaining provider-free Section 5 gaps without introducing a second registry or provider authority.

## Converged residuals

1. Market-event adapter build provenance
- every normalized MarketEvent carries a bounded canonical adapter_version;
- event identity and stream/correction chronology bind the adapter build;
- a build change inside one active generation fails before state mutation;
- a new build is admitted only under an explicit newer generation.

2. Causal information-claim supersession
- later causally visible source revisions supersede earlier revisions without deleting history;
- delayed ingestion of old revisions cannot roll truth backward;
- current cross-source disagreement is surfaced instead of silently selecting a winner;
- conflicting values from the same immutable source revision fail closed.

3. Frozen authoritative data population
- causal source/population lookup re-resolves exact dataset/version/manifest/cut;
- authenticated ArtifactStore evidence and rights identity are required;
- dangling content hashes and forged caller-created population values fail closed;
- causal feature/source values bind one frozen authoritative population.

## Dependency

The contract/schema half depends on Section 1 contract v6. This PR is intentionally stacked on Section 1 until it is accepted, then must be retargeted/reconverged to accepted main and requalified.

## Closure requirements

Section 5 is DONE only after:
1. Section 1 accepted;
2. this candidate is current-main and ahead-only;
3. exact-head contracts, research-primitives, baseline and Verify are terminal green;
4. review state is clean;
5. merge completes;
6. post-merge readback confirms accepted source identity.

No provider/PAPER/LIVE, profitability, economic-edge, release or NVDA qualification is granted.
