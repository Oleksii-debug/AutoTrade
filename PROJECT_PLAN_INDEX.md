# AutoTrade — Canonical Multi-Plan Index

## Authority

The former monolithic 48-Section execution plan is **SUPERSEDED FOR WORK SELECTION**.
It remains historical/audit evidence only.

Canonical Drive folder:
https://drive.google.com/drive/folders/1c4iSaT9hxgGbEzROpsGkedpZzVlzllsc

Plans 1–7 are independent engineering plans with no priority order.
Plan 8 is provider-free M1 whole-product convergence.
Plan 9 is the final dependency-aware release/NVDA/M2 plan with an optional later external-provider activation track.

## Plans

1. Перший план — Фінансове ядро, портфель, ризик та економіка
https://docs.google.com/document/d/1-QU64I32cFepZwYcxINwRuYIe9ptRSBA5xVqCtquC1s/edit

2. Другий план — Агент, дослідження, навчання та моделі
https://docs.google.com/document/d/1EWWvsf4uLhRU881lr8z15rndBiox9VqIry0GeY_3JHU/edit

3. Третій план — Runtime, відновлення, backup, Host та observability
https://docs.google.com/document/d/1UBKbVuUQf7CIieqNiEfn144t_CvWXkWPstthLYdTM7Y/edit

4. Четвертий план — Довіра, безпека, хронологія та supply chain
https://docs.google.com/document/d/1byiGz1q8f4GpBeTiMUwS6TiN6hTqoXclkIxTFM19NUQ/edit

5. П’ятий план — Web, Windows Desktop, accessibility та packaging
https://docs.google.com/document/d/1D-O-uLOe9DfddFkZA3d_icjKPmsR0qwImm14mGJ7imk/edit

6. Шостий план — Providers, account/capability contracts та adapters
https://docs.google.com/document/d/1mG2hK9mVdu73JkZmbUDEazE9RW162bj3VjFXUUzqCDI/edit

7. Сьомий план — Наукова кваліфікація, evidence та performance
https://docs.google.com/document/d/1Di1lglr6A_NRw2I3hvKy2vXF3WiV2eLSlpbjW4TlQQ4/edit

8. Восьмий план — Provider-free M1 whole-product convergence
https://docs.google.com/document/d/1pebBXU4mdFMnFpCC7cc1fStkpV1vnUNr4U4EppOKqns/edit

9. Дев’ятий план — External qualification, signed release, NVDA та M2
https://docs.google.com/document/d/1sbCFL7NeCK5AfUvZFkGJ2FbOGBAAfHqPcj5-99j9qFw/edit

## Worker selection

For Plans 1–7:
- read this index, MULTI_PLAN_PARALLELISM_CONTRACT.md, MULTI_PLAN_CLOSURE_STATE.md, assigned Drive plan and live GitHub;
- use MULTI_PLAN_CLOSURE_STATE.md as live status authority;
- skip terminal DONE;
- work the first ACTIONABLE unfinished Section inside the assigned plan;
- plans may start and finish in any order;
- existing PR/branch/source must be REUSE -> REPAIR -> CONVERGE before creating duplicate scope.

Plan 6 is independent and ACTIONABLE offline without provider/account credentials. Use existing code, public specs and fixtures; real account/PAPER/LIVE activation is deferred to the optional Plan-9 provider track.

Plan 8 begins terminal M1 convergence only after Plans 1,2,3,4,5,7 have terminal outputs required for M1. Plan 6 is not an M1 dependency.

Plan 9 uses per-Section dependencies. Provider Sections 2–6 are optional parked external activation and do not block a provider-free release. WAITING/PARKED Sections are skipped without false DONE; take the first ACTIONABLE Section whose named gates are satisfied.

## Migrated accepted work

Legacy Sections 0–1 and 3–14 retain their accepted DONE engineering evidence in their new owners.
Legacy Section 2 is split:
- repository-controllable dependency/provenance engineering -> Plan 4 / Section 2, internally complete;
- final external/delivered-release rights/trust/SBOM facts -> Plan 9 / Section 1, WAITING_EXTERNAL.

Legacy Section 15 -> Plan 1 / Section 10, QUALIFYING on canonical PR #2318.
Legacy Section 16 -> Plan 2 / Section 2, PARTIAL_EXISTING on PR #1631.
Legacy Section 17 -> Plan 1 / Section 11, PARTIAL_EXISTING on PR #1628.

Detailed 0–47 mapping: LEGACY_48_TO_MULTIPLAN_COVERAGE.md.

## Canonical UI decision — 2026-10-08

The canonical AutoTrade interface is the browser-like semantic Web UI (`web/src`) with page/section navigation, headings/landmarks, forms, tables, live status and keyboard/NVDA-first behavior. The Windows Desktop surface embeds this same Web artifact and adds only host integration/native emergency fallback; it must not fork a second navigation model.
