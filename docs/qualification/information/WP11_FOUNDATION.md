# WP-11 — причинний фундамент інформаційних тверджень

Статус: **FOUNDATION_ONLY / NOT_FULLY_QUALIFIED**

Цей доказ стосується лише канонічної відповідальності WP-11 `INFORMATION / news-macro-claims`.
Він не надає торгової, релізної, мережевої або модельної влади й не є доказом economic edge.

## Канонічна база та точна implementation evidence revision

- canonical PR: #399
- reconverged base: `main@92ca4e0cfd8949645663eeaf092ac2db156ec75a`
- implementation evidence head: `0cd0b9d83a1a072e0a9451f991def2225db2a840`
- contract manifest version на цій базі: `1.0.0`
- `mvp/autotrade_mvp/information_claims.py` blob: `c253bc33b66b78f016ab23cb4ef96c013a821bae`
- `mvp/tests/test_information_claims.py` blob: `76a9bab2bf2f8caf4e34c02fc9532acf80dc991b`
- `tests/Contracts/test_information_evidence_contracts.py` blob: `f0026c6bbc5d5df185943f77dded0cd2bf4b3050`

Цей файл оновлюється evidence-only commit після наведеної implementation revision; тому SHA поточного PR після цього документа закономірно відрізнятиметься, а наведені code/test blobs лишаються точними для перевіреної реалізації.

## Реалізовані інваріанти

- зовнішній текст завжди `untrusted_content=True` і `permission_effect=NONE`;
- прямі `SourceDocument` та `InformationClaim` constructors не обходять текстові, source-kind, UTC, SHA-256 або authority-інваріанти;
- `published_at <= available_at <= ingested_at`; decision/replay cutoff бачить claim лише коли і зовнішня availability, і фактичне ingestion уже настали;
- duck-typed fake source document не допускається до claim construction;
- syndicated duplicates мають content-semantic identity, незалежну від outlet publication clock;
- canonical deduplicated view зберігає найранішу фактичну causal visibility, але окрема immutable provenance history не стирає жодної source revision;
- batch extraction key = `(source_id, source_revision)`, тому однакові labels на кшталт `r1` у різних джерелах не змішують extracted values;
- конфліктні claims не перезаписуються;
- snapshot fail-closed для future/un-ingested claims, duplicate claim IDs, syndicated duplicates та non-canonical ordering;
- future revision не змінює вже сформований ранній snapshot digest;
- `InformationClaim.evidence_digest()` комітить content, provenance, rights, availability та ingestion timestamps;
- `SourceDocument.to_evidence_ref(...)` приймає explicit artifact UUID + exact artifact SHA-256 і проектує їх у canonical `EvidenceRef` v1.0.0;
- `build_information_event(...)` проектує provenance-matched claims у canonical `InformationEvent`, вимагаючи explicit numeric revision, language, extraction version, exact confidence population та artifact evidence;
- mixed-source claims, missing/extra confidence, forged digest/UUID/URI та causal-time violations fail-closed;
- snapshot manifest не копіює raw passage або extracted value.

## GitHub evidence на exact implementation head

### Baseline

Workflow run `36103893106` на `0cd0b9d83a1a072e0a9451f991def2225db2a840`:

- Ubuntu: success
- Windows: success
- overall baseline: success

### Canonical contract projection

У full Verify run `36103893092` чотири WP-11 contract tests були фактично виконані **до** глобального stop і пройшли на обох ОС:

1. `test_information_event_projection_matches_canonical_contract` — OK
2. `test_information_event_projection_rejects_mixed_or_incomplete_provenance` — OK
3. `test_source_artifact_projection_matches_canonical_evidence_ref` — OK
4. `test_source_artifact_projection_fails_closed_on_invalid_provenance` — OK

На Ubuntu та Windows contract phase виконав 33 тести; WP-11 tests вище були green.

### Чому whole Verify все ще red

Той самий run `36103893092` завершується двома provider-contract errors, які вже відтворюються на canonical main і не належать WP-11:

- Bybit V5 contract test викликає `parse_submission_response()` без нового required keyword-only `environment`;
- Kraken Futures `SubmissionResult` містить `observed_at`, якого поточний canonical provider schema не дозволяє.

Через fail-fast після contract phase exact-head GitHub run **не виконав** `mvp/tests/test_information_claims.py`. У цьому файлі зараз 23 focused tests, але цей документ не видає їх за GitHub-green evidence.

Історичний локальний доказ `Ran 13 tests / OK` стосувався старішої реалізації та більше не є достатнім доказом для поточного head.

## Що цей доказ НЕ доводить

- реальне network ingestion новин, macro або corporate feeds;
- source discovery, reconnect, rate-limit, retry/outage та backfill semantics;
- правомірність/ліцензійність конкретних зовнішніх джерел;
- реальний artifact-store write/read/recovery через WP-06; поточний WP-11 лише приймає exact artifact identity на projection boundary;
- durable information store, crash/restart recovery та correction invalidation end-to-end;
- production `src/AutoTrade.Information/` integration; поточна реалізація все ще MVP foundation;
- повноту entity/event/quantity/unit extraction;
- точність автоматичного або model-assisted extraction;
- source registry lifecycle, correction policy та trust-feature learning;
- causal leave-source-out / delayed-source ablations;
- економічну прогнозну цінність будь-якого джерела;
- WP-63 marginal-value evidence;
- whole-product або WP-11 full qualification.

Поки ці межі не закриті прийнятими exact-head доказами, WP-11 не можна позначати повністю завершеним.
