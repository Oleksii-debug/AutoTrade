# WP-11 — причинний фундамент інформаційних тверджень

Статус: **FOUNDATION_ONLY / NOT_FULLY_QUALIFIED**

Цей доказ стосується лише канонічної відповідальності WP-11 `INFORMATION / news-macro-claims`.
Він не надає торгової, релізної, мережевої або модельної влади.

## Точна джерельна ревізія

- PR: #206
- source commit: `c69404cfae66888ee817ed4e45dd7a6bb5ff7cd1`
- reconverged base: `main@1e4632906788d7b0f14567cc68bab8b53764946d`
- `mvp/autotrade_mvp/information_claims.py` blob: `7c0b40f0cd5f3c49e52a0b30344a79eb032e7cba`
- `mvp/tests/test_information_claims.py` blob: `d6371554bfc1957842f4453eab85d2ff380f16a1`

## Контракти, які перевірено

- зовнішній текст завжди `untrusted_content=True` і `permission_effect=NONE`;
- прямий конструктор не може обійти канонічні SHA-256, часові або authority-інваріанти;
- `available_at >= published_at`, часові значення нормалізуються до UTC;
- syndicated duplicates дедуплікуються, суперечливі твердження не стираються;
- ревізії джерела зберігають історію;
- твердження після causal cutoff невидимі раніше;
- `InformationClaim.evidence_digest()` комітить зміст, provenance, rights і causal timestamps;
- `InformationSnapshot` має schema version `1.0.0`, комітить cutoff і лише digest identities;
- майбутня ревізія не може змінити digest уже сформованого раннього знімка;
- після causal availability нової ревізії digest змінюється;
- прямий snapshot fail-closed для future, duplicate та non-canonical claims;
- snapshot manifest не копіює сирий passage або extracted value.

## Фактично запущена локальна перевірка

Команда:

```text
python -m unittest mvp.tests.test_information_claims -v
```

Результат для точних локальних файлів, чиї Git blob SHA наведено вище:

```text
Ran 13 tests
OK
```

Перед GitHub-записом обидва створені blob SHA були звірені з локально протестованими Git blob SHA.
Після реконвергенції порівняння з `main@1e4632906788d7b0f14567cc68bab8b53764946d` показало `behind_by=0` і зміни лише у двох WP-11 файлах.

## Що цей доказ НЕ доводить

- реальне мережеве отримання новин, макроданих або корпоративних матеріалів;
- правомірність/ліцензійність конкретних зовнішніх джерел;
- повноту source discovery, reconnect, rate-limit або outage поведінки;
- durable persistence та відновлення інформаційного сховища після crash;
- інтеграцію з остаточними `InformationEvent` / `EvidenceRef` контрактами;
- точність автоматичного extraction або model-assisted extraction;
- економічну прогнозну цінність будь-якого джерела;
- causal ablation / source marginal value з WP-63;
- повну кваліфікацію WP-11.

Поки ці межі не закриті прийнятими доказами, WP-11 не можна позначати повністю завершеним.
