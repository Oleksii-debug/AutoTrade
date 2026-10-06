# WP-18 provider-neutral execution foundation — source-complete qualification-pending cut

Canonical integration PR: #2313
Exact product/test source SHA covered by this finding: ce18d67718d06eb346378973edc5843f38d4447e
Exact base main for that source SHA: b2e353709ea2e0fdd57c4152467060540bedb67b

## Source acceptance implemented

- durable SubmissionPrepared before irreversible send;
- final authority, sender and capability rechecks before wire;
- stable provider-compatible client identity;
- irreversible SubmissionSending barrier;
- timeout, post-barrier exception, ambiguous response and crash recover as UNKNOWN with no blind retry;
- exact response bytes/status/digest/encoding survive durable replay;
- exact-marker SENT and UNKNOWN response bindings are revalidated;
- raw transport success is not lifecycle ACK/fill authority;
- ACK never invents a fill;
- WP-20 reconstructs ambiguous dispatch with durable attempt/client/account/environment/provider-domain/owner identity and zero resend;
- direct trading-write receipt is non-self-mintable;
- direct Bybit authenticated-read receipt binds exact provider_environment, canonical query, request digest, request-semantics digest, response status/digest and direct network policy;
- exact parsed direct-read observation is closure-bound to its direct receipt;
- Host/provider canonical JournalStore identity is unified;
- Host direct-wire pin conjunction rejects plain TEST/INJECTED observations and tampered observations;
- read capability expiry during quota wait is zero-secret / zero-wire;
- read capability supersession during quota wait is zero-secret / zero-wire;
- write capability expiry during quota wait is zero-secret / zero-guard / zero-wire;
- direct Bybit read expiry during quota wait is zero-secret / zero-wire;
- direct Bybit read rechecks current capability after the unbounded provider-I/O window before accepting receipt/observation.

## Qualification evidence pending

Hosted exact-head baseline, provider-free-product, dotnet-foundation, reconvergence-integrity and Verify AutoTrade runs are registered for the current PR lineage. At this source SHA they are queued/pending, therefore are not PASS.

Local checkout/test execution is unavailable in this execution environment because github.com DNS resolution fails.

## Explicit non-claims / downstream boundaries

- no real provider credentials were used;
- no real provider order/read campaign was run;
- no PAPER/LIVE or real-money authority is granted;
- no exactly-once external execution claim is made;
- positive provider-specific Host/wire -> durable PROVIDER_ORIGIN promotion and provider campaign qualification remain under #652;
- profitability/economic edge, release readiness and NVDA/Windows product qualification are separate gates.
