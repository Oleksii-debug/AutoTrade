# Legacy 48-Section -> Multi-Plan Coverage Matrix

This is a completeness audit artifact, not an execution plan.

- Legacy Sections checked: 48 / 48.
- Unmapped legacy Sections: 0.
- Plan count: 9.
- Plans 1–7 are independent engineering plans.
- Plan 8 is provider-free M1 convergence.
- Plan 9 owns external/provider/release/NVDA/M2 qualification.

A legacy Section can map to multiple new Sections when the old scope mixed reusable engineering with final/external qualification.

| Legacy Section | New owner(s) |
| ---: | --- |
| 0 | Plan 4 / Section 1 |
| 1 | Plan 1 / Section 1 |
| 2 | Plan 4 / Section 2 + Plan 9 / Section 1 |
| 3 | Plan 3 / Section 1 |
| 4 | Plan 3 / Section 2 |
| 5 | Plan 1 / Section 2 |
| 6 | Plan 2 / Section 1 |
| 7 | Plan 3 / Section 3 |
| 8 | Plan 1 / Section 3 |
| 9 | Plan 1 / Section 4 |
| 10 | Plan 1 / Section 5 |
| 11 | Plan 1 / Section 6 |
| 12 | Plan 1 / Section 7 |
| 13 | Plan 1 / Section 8 |
| 14 | Plan 1 / Section 9 |
| 15 | Plan 1 / Section 10 |
| 16 | Plan 2 / Section 2 |
| 17 | Plan 1 / Section 11 |
| 18 | Plan 2 / Section 3 |
| 19 | Plan 7 / Section 1 |
| 20 | Plan 2 / Section 4 + Plan 7 / Section 2 |
| 21 | Plan 2 / Section 5 |
| 22 | Plan 2 / Section 6 |
| 23 | Plan 2 / Section 7 |
| 24 | Plan 3 / Section 4 |
| 25 | Plan 3 / Section 5 |
| 26 | Plan 4 / Section 3 |
| 27 | Plan 4 / Section 4 |
| 28 | Plan 3 / Section 6 |
| 29 | Plan 3 / Section 7 |
| 30 | Plan 5 / Section 1 |
| 31 | Plan 5 / Section 2 |
| 32 | Plan 5 / Section 3 |
| 33 | Plan 5 / Section 4 |
| 34 | Plan 5 / Section 5 |
| 35 | Plan 4 / Section 5 + Plan 7 / Section 3 |
| 36 | Plan 7 / Section 5 |
| 37 | Plan 8 / Sections 1–6 |
| 38 | Plan 6 / Sections 1–2 |
| 39 | Plan 6 / Section 3 |
| 40 | Plan 6 / Section 4 + Plan 9 / Section 3 |
| 41 | Plan 9 / Section 4 |
| 42 | Plan 9 / Section 5 |
| 43 | Plan 9 / Section 6 |
| 44 | Plan 5 / Section 6 + Plan 9 / Section 7 |
| 45 | Plan 9 / Section 8 |
| 46 | Plan 7 / Section 6 + Plan 9 / Section 9 |
| 47 | Plan 9 / Section 10 |

Current status is always read from `MULTI_PLAN_CLOSURE_STATE.md`.
Safe mutation ownership is defined by `MULTI_PLAN_PARALLELISM_CONTRACT.md`.
