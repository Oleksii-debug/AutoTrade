# AutoTrade — baseline finalization audit

Audit date: 2026-09-22. Starting main: `adee949cb7023505285d26cf1da3cd61f8e53341`. The exact delivered revision is recorded in the generated `SOURCE_REVISION.json`; current implementation state is in `control/qualification.json`.

## 1. Findings and disposition

| Finding | Disposition | Evidence / practical limit |
|---|---|---|
| Drive documents and pre-bootstrap ZIP are stale | Preserve them as history; export a complete new snapshot from committed Git | All archive members checked against SHA-256; HTML includes 14 and the finalization guides |
| User-reported LEAN object ambiguity | Already corrected in GitHub 00/01/08/09 before this audit; confirmed again against upstream API and consolidated in provenance | Commit `985ef30ad3ac774218c5ac516b4cb0aa2655730f`, tree `4b163abf9fca60e731b76510b9ae6721ffff7e6c`; no automatic adoption of newer main |
| Document 14 still promised later research; precedence retained temporary wording | Close its finite evidence scope, integrate SDK implications into 00/01/09 and relevant bank entries, add concrete next steps in 15 | Inspected source and candidate status remain distinct from build/provider tests |
| `execution.schema.json` was not valid JSON | Restore missing object closure and readable formatting; add resolution check for every schema reference | Existing contract suite now executes; this does not complete cross-language WP-01 |
| Registry overlap compared literal strings only and depended on matching labels | Compare canonical parent/child repository paths across semantic labels, conservatively case-insensitive for Windows | Regression cases plus disjoint-path case |
| Malformed active leases could disappear from collision checks | Validate claim payloads and generations before transitions | Invalid registry state fails closed |
| Lost claim reply could not be replayed after generation/lease changes | Retain original request and replay recorded claim without creating new authority | Replayed expired/released records are history; caller must check current lease/status |
| Branch guard only needed partial scope overlap | Require current owner coverage of every changed path | Unowned additional scope is rejected |
| Registry state functions had no actual atomic storage primitive | Add Git store that commits state and append-only log together and only publishes a single-parent non-force successor | Local real-Git integration test proves one sibling winner, safe retry and retained history. This is not a deployed authenticated registry service |
| CI described only Python and ignored an initial locked-restore error | Correct status and separate honest bootstrap restore from future dependency-lock gate | No ignored restore failure; release-grade .NET locks are still required by WP-03 |
| Machine bank and prose could drift | Generate document-08 entries from the existing JSON bank; validate 65 IDs, acyclic dependencies and INDEX targets | One canonical bank, reproducible reading copy |

## 2. Verification actually performed

Local environment: Linux, Python 3.12.14, real Git subprocesses. Control tests cover state semantics and storage publication; contract tests validate schemas, references and fixtures; research tests exercise the migrated neutral primitives. Exact commands and outcomes are retained in `docs/qualification/2026-09-22-local-baseline.json`. The archive verifier checks member completeness, byte counts, SHA-256 and ZIP CRC. HTML fragment links are checked mechanically.

GitHub Actions existed but were queued at initial inspection. A queued run is neither a failure nor a pass. Local results do not substitute for Windows, .NET SDK, real broker, installer or NVDA results. No provider keys, real orders or autonomous withdrawals were used. No new claim of investment performance is made.

## 3. Remaining concrete gates

1. Run configured CI on Windows and Linux; inspect exact-head results. .NET was unavailable in this local environment, so the C# build is not claimed here.
2. Both `main` and `control/registry` were unprotected and repository rulesets were empty at inspection. A narrow service identity, authenticated owner mapping, trusted service time, bank readiness/dependency validation, per-claim fencing, authenticated branch-writer evidence and protected non-force publication are still needed before enabling concurrent mutation. The store is a reusable primitive, not that whole service.
3. Complete WP-01 common cross-language corpus/code generation and WP-03 exact release dependency/rights composition. The selected first-party modules have owner-authorized project migration records; public repository status is not a blanket license.
4. Continue document-15's LEAN/simulator/financial-spine path; the full runtime is not initialized merely because the transfer baseline is complete.

These gates are represented as open work. The repository remains usable for authorized single-writer implementation while they are resolved. No global permanent worker limit or country-based provider exclusion is introduced.
