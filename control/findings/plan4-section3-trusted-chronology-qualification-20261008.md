# Plan 4 / Section 3 — trusted chronology qualification (2026-10-08)

## Scope and immutable claims

Canonical Plan 4 Section 3 is repository-controllable component engineering, **not** physical external time attestation or a released product. The existing WP-48 source is already on main; no alternative clock authority, JournalStore, trust root, signing service, sender, or production host is created.

Initial exact source base: `main@e48888caaa741b5eb0c228027da7e150a3fa5ca0`. Canonical source: `mvp/autotrade_mvp/trusted_chronology.py`, `trusted_chronology_cut.py`, `_trusted_chronology_cut_impl.py`, `recovery_clock_incident.py`. Integrated predecessor: merged WP-48 PR #2009; earlier #2005/#2006/#2007 and #2015 are evidence references, not new authorities.

## Existing invariants under cross-platform qualification

- SOURCE_QUALIFICATION is runtime-free; RELEASE_RUNTIME binds current production host occurrence, store identity, recovery owner and exact release artifact ID/hash.
- Challenge nonce, source SHA, journal frontier and clock-incident generation bind a strictly parsed external UTC transcript.
- Conservative acceptance rejects reversed/malformed time, excessive uncertainty/offset/freshness, predated receipt signatures and future-claimed event PASS; independent signed evidence is required.
- Append-only challenge/accepted cut publication and durable clock incident invalidation survive restart. Mutations during verifier callbacks, store/owner swaps, runtime-occurrence supersession and standalone caller-owned horizon are rejected.
- No test fixture grants provider, PAPER/LIVE, financial truth or external-release qualification.

## Evidence gate

`.github/workflows/plan4-trusted-chronology.yml` runs the actual existing negative, signed evidence, crash/restart, host occurrence and adversarial test suites on **Ubuntu and Windows** at each PR head. A missing, cancelled, queued or failed exact-head result is **not PASS**. Main baseline / cross-platform Verify failures outside this specific scope must be reported, not concealed.

This record intentionally does **not** set terminal DONE; the canonical registry and Drive plan are updated only after source integration and exact-head qualified evidence readback.
