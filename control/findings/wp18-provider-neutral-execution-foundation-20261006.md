# WP-18 provider-neutral execution foundation — qualification evidence

Date: 2026-10-06

## Exact source cut

- Repository: `Oleksii-debug/AutoTrade`
- Integration PR: #2305
- Product-source SHA covered by this evidence: `075e4dc9b68f9e1ed25516c907ce4a2d382b710e`
- Base main SHA: `9544ca69d1593c8a880956504591122c42771f16`
- Work package: `WP-18 / EXECUTION / guarded-dispatch`
- Control schema: `control/INDEX.json schema_version 1.0.0`

This evidence is deliberately source-SHA specific. A later source mutation requires a refreshed evidence record.

## Canonical inputs

- `docs/engineering/02_CANONICAL_CONTRACTS.md`
- `docs/engineering/03_DATA_MARKET_PROVIDER_EXECUTION_ARCHITECTURE.md`
- `docs/engineering/14_ADDITIONAL_READY_CODE_RESEARCH.md`
- `docs/engineering/15_FAST_IMPLEMENTATION_START.md`
- exact accepted WP-02/WP-17 artifacts reachable from current `control/INDEX.json`
- `control/work-packages/bank.json` WP-18 definition

## Implemented execution truth

The exact source cut contains one provider-neutral guarded-dispatch authority with:

- durable `SubmissionPrepared -> SubmissionSending -> SubmissionSent|SubmissionUnknown` chronology;
- persist-before-send;
- final authority/sender recheck immediately before the irreversible barrier;
- deterministic, provider-compatible client identity;
- no blind retry after possible send;
- exact response bytes/status/digest/encoding binding;
- ambiguous/opaque/timeout/post-barrier failures retained as UNKNOWN + reconciliation-first;
- restart replay that does not re-authorize or resend a possibly-sent attempt;
- duplicate-intent/outbox fences;
- quota-delay expiry/revoke behavior with zero wire before the barrier;
- post-barrier revoke/uncertainty remaining UNKNOWN;
- stable WP-19 handoff where transport ACK is not lifecycle truth and ACK is not a fill;
- WP-20 recovery identity binding for attempt/client/account/environment/provider_environment/owner/chronology;
- production-shaped Bybit one-wire adapter coverage;
- sealed direct-write transport receipt with direct-only network policy identity and lower urllib/http/socket/TLS authority;
- fail-closed authenticated-read provider-domain and Host-attestation prerequisites without promoting TEST/INJECTED evidence to financial PROVIDER_ORIGIN;
- Host-signed provider-origin bridge cross-binding signed subject/receipt/durable rows to the selected canonical JournalStore generation, without inventing a second journal or caller-mintable token;
- exact empty post-SEND write evidence: empty HTTP 200 and legacy empty raw bytes remain UNKNOWN/reconciliation-first across Bybit, Kraken Spot, Alpaca, Binance Spot and WhiteBIT;
- immutable provider-response resource ceiling/helper authority and exact built-in provider text/credential/query-value ingress;
- lower urllib/http/socket/TLS direct-write authority so receipt mint cannot survive rebound HTTPSConnection/socket/TLS roots.

## Executable falsifiers present in the exact source

Representative direct tests include:

- crash before/after send barrier and restart/no-resend matrix;
- timeout/429/5xx/status-missing/opaque response -> UNKNOWN;
- concurrent/replayed duplicate-attempt and same-intent fences;
- deterministic bounded token and UUID client IDs;
- capability expiry during quota wait -> zero wire;
- host revoke during quota wait -> zero wire;
- post-barrier host revoke -> UNKNOWN;
- successful quota wait -> exactly one outbound request;
- exact response binding replay and tamper rejection;
- malformed/duplicate-key/non-finite/deep/oversized provider-response rejection;
- provider-neutral SENT/UNKNOWN -> OMS UNKNOWN until authenticated provider-specific normalization;
- ACK != fill and independent fill evidence;
- durable ambiguous dispatch -> WP-20 UnknownSubmission after restart with zero resend;
- BYBIT TESTNET/DEMO provider-domain non-aliasing;
- direct-write receipt constructor/forgery/injected-opener rejection;
- HTTPSConnection/socket/default TLS factory rebinding invalidates direct-write receipt authority;
- Host attestation SPKI encoded-length and response-base64 pre-decode resource fences.

## Tests actually run

For this exact source SHA, GitHub hosted workflows were requested by the pull request:

- `baseline`
- `provider-free-product`
- `Verify AutoTrade`
- `dotnet-foundation`
- `reconvergence-integrity`

At evidence creation time they are **QUEUED**, not PASS. Repository-wide Actions state showed hundreds of queued runs and only a few long-running workers. No local checkout/test execution is claimed because this execution environment cannot resolve `github.com` for git/container networking.

Earlier donor heads are not promoted to qualification evidence merely because their source was reviewed; their hosted runs were also queued/cancelled. Source review and byte-identical convergence are evidence of composition, not substitutes for terminal test results.

## Non-loss convergence evidence

Before this evidence record:

- 24/24 execution-foundation non-overlap blobs matched the canonical execution donor;
- 4/4 provider-domain/provider-origin-journal blobs matched their donor;
- 10/10 Host-attestation blobs matched the latest accepted Host donor at the time of absorption, with later Host resource-fence hardening explicitly absorbed;
- provider transport is a semantic composition:
  - direct-write/lower-network authority block exact-matches the latest direct-wire donor;
  - Bybit authenticated-read rule and signer/transport blocks exact-match the provider-domain donor;
- lower-network direct-wire tests were absorbed byte-identically.

## Unresolved limits / non-claims

- Hosted exact-head qualification is not terminal yet; queued/cancelled/missing is not PASS.
- No real credentials or live/PAPER provider campaign were used.
- No claim of exactly-once external execution is made.
- No broker correctness, profitability/economic edge, release readiness or real-money authorization is made.
- The Host-signed/canonical-journal verification bridge is present, but the real Host→canonical-Python-JournalStore durability callback and fully bound real credential-bearing authenticated-read wire remain downstream under #652. This source stays fail-closed rather than manufacturing that authority.
- Provider-specific SDK/network expansion beyond the shared transport is outside this provider-neutral foundation unless separately owned/qualified.

## Merge rule

Do not mark WP-18 provider-neutral foundation qualified or merge solely from this finding. First require terminal PASS for the exact qualification head on the repository-required gates. If source changes, refresh this evidence with the new source SHA.


## Residual lineage closure

The current source explicitly subsumes the still-relevant semantics from historical WP-18 lines:

- #2012: response-resource helper/hard-ceiling authority, raw empty write evidence, empty HTTP 200 reconciliation-first classifiers;
- #2013: exact provider text/credential/signing-value ingress;
- #2022: exact inert authenticated-read query/capability/time/text construction;
- #2023: Kraken/Binance authenticated-read binding construction authority;
- #2307: lower urllib/http/socket/TLS direct-write receipt authority;
- #2308: Host-signed subject/receipt to canonical JournalStore verification bridge.

Those source PRs are historical evidence, not parallel integration parents.
