# Canonical baseline and evidence precedence

The LEAN identity correction and document-14 SDK findings are integrated into 00/01/08/09 and `control/work-packages/bank.json`. There is no temporary competing LEAN truth.

- `control/INDEX.json` locates the current requirements, contracts, work bank, qualification and registry.
- Approved product/engineering contracts define intended behavior. Implementation and exact-source test evidence establish actual behavior; a defect in code does not silently supersede the requirement.
- The JSON work bank owns package fields. Document 08 is generated from it. Status is evidence-bound and may remain IN_PROGRESS despite merged code.
- `provenance/components.json` owns recorded source identities. LEAN revision type is commit; source tree is a separate field.
- `control/qualification.json` owns gate state. The live registry branch owns development claims only when the deployed service is qualified. Snapshot copies do not grant ownership.
- `SOURCE_REVISION.json` and `MANIFEST_SHA256.json` identify the exported Git revision and exact bytes. A newer Git commit is a new baseline; rebuild exports after accepted changes.
- Old Drive snapshots and old ZIPs remain history. No artifact is treated as current solely because its filename says final/current.

When behavior, schemas and requirements disagree, record the discrepancy and resolve it explicitly. Neither newest prose nor newest code automatically grants trading authority or proves correctness.
