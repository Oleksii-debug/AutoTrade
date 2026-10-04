# Інтеграція офлайн-циклу розділів 5–9

## Точна база й походження

База: `main@a792448cf7a9265ea0ed70a09d33d5e0f955f33b`.
Ізольована інтеграційна гілка: `integration/offline-loop-5-9-20261004`.
Власник продуктового рішення — Oleksii-debug. Ця робота виконує пряму команду
власника на інтеграцію; гілки власників компонентів не змінюються.

Повторно використано наявні реалізації, без другого обліку, OMS або risk engine:

| PR | Точний вхідний SHA | Реалізація |
| --- | --- | --- |
| #1488 | `45dfad1ac7e538f98fa3f1f18aa29405bb258a02` | settlement і локальний доступний капітал |
| #1472 | `3ed596aafea634d4af5d5798970e7c2e0eb98558` | повний runtime checkpoint |
| #1478 | `b97c5037b262d086b505d9196f08e7e33b41b472` | відновлення збереженого internal fill |
| #1502 | `c40af5eb137e8f4a00cf7713f65d3e0b8bcbbb94` | перевірка авторитетного replay snapshot до підпису |

Під час фінальної перевірки додатково перенесено лише нові capital-causal
зміни `authority.py` і їхні регресії з #1488
`3eb7beee9dbedfc2ac57da2acfd7622c69dd2cac`. Callback-free registry та
власні integration repairs збережено; поточний чужий head не перезаписано.

## Продуктові виправлення інтеграції

1. Прив'язка `AuthorityService.store` встановлюється до перевірки складеного
   settlement/economic owner. Справний офлайн-ордер проходить той самий risk gate.
2. Відновлення availability порівнює канонічні точні десяткові записи;
   `Decimal` не порівнюється з JSON-рядком як інший фінансовий стан.
3. Історична перевірка використовує збережений journal cut прийнятого рішення.
   Пізніший settlement не переписує історію. Новий dispatch окремо перевіряє
   поточні дані. `require_latest_scope=True` забороняє історичний cut.
4. Відновлення збереженого fill створює той самий economic transaction і
   settlement obligation, що звичайне виконання, у спільному атомарному commit.
5. Checkpoint включає `settlement_book`, а також журнал, ордери, резервування,
   provider image, strategy prefix, active policies, valuations і frozen inputs.
   RNG у цьому детермінованому ZERO-циклі явно `NONE`; модель не викликається.
6. Приватний випадковий ключ checkpoint залишається прив'язаним до durable start
   і підпису, але не змінює детермінований public protocol digest однакових запусків.
7. Календарний порядок офлайн-епізоду задекларований у protocol v7:
   settlement на event time, потім decision/fill на event time + 1 microsecond.
   Затримка settlement рахується від початку ринкового епізоду; майбутня ціна
   або наступний епізод при цьому не відкриваються.
8. `AutonomousEpisodeCompleted` атомарно зберігає підписаний receipt точного
   preimage, результату і наступного journal cut. Після аварії між durable commit
   і публікацією sidecar дозволено лише відновлення саме цього terminal cut.
   Інші записи журналу, змінений receipt, unsafe leaf або неправильний підпис
   відхиляються. Pending outbox самого terminal event перевіряється й завершується.
9. Власник service утримує capital books; приватний registry використовує
   weakrefs без callback. Звільнення service звільняє книги, без eraser callback.
10. Операторський звіт читає канонічні книги й окремо показує account cash,
    settled cash, unsettled receivable/payable, reserved cash, available cash,
    FIFO realized/unrealized P&L і cost basis. Перевіряються cash conservation
    та equity change = gross realized + gross unrealized − fees для цього циклу.
11. Protocol v7 заморожує `execution_profile` і `target_quantity`. Наявний
    `SimulatedProvider` виконує ордер одним fill або двома рівними partial fills;
    крок інструмента лишається 1. Перед створенням стану перевіряються повний
    обсяг і кожна частина. Кожний fill окремо атомарно змінює OMS, економіку,
    резерв і settlement obligation. Recovery приймає лише точний фінансовий
    prefix збережених fills, перевірений проти історичного admission/send.
    Повторний запуск не створює нових order, fill, fees чи obligations.
12. Capital cut охоплює provenance read і economic projection в одному
    стабільному journal cut. Всі economic facts мають передувати provider query,
    а їхній journal head — точному reconciliation checkpoint. Старий cash
    admission без потрібного capital receipt не відновлює dispatch authority.
    Deposit між history read і projection відхиляється з нульовим резервом.
13. Початкові гроші отримують детермінований committed_at: start time для
    autonomous loop і одну microsecond перед query для single-episode bootstrap.
    Фізичний час запуску процесу більше не стає часом simulated seed economics.
14. Post-merge audit виявив, що autonomous policy registration ще читала
    фізичний час. Вона тепер використовує наявний SIMULATION-only
    `AuthorityService.register_policy(..., simulation_time=timestamp)`.
    Регресія забороняє wall-clock read для durable policy registration і вимагає
    однакових policy event IDs, timestamps та payload hashes після uninterrupted
    і paused/resumed запусків для обох execution profiles.
    Це draft continuation: фінальний результат прогону не підтверджено після
    втрати відповіді execution service; новий PASS не заявлено.

Приклад того самого продуктового CLI, без іншого engine:

```text
python -m mvp.autotrade_mvp.cli --autonomous-simulation --state-dir zero-partials --episode-id partials --at 2026-10-03T00:00:00Z --prices 100,101,103,90,110,120,121 --execution-profile TWO_EQUAL_PARTIALS --target-quantity 2
```

## Перевірка

Окремі процеси перевіряють справжній `os._exit(23)` після terminal commit,
перезапуск із pending outbox, відсутність повторного send/fill/списання,
і Windows-сумісний шлях із пробілом та кирилицею. Це Linux process evidence,
а не native Windows або human NVDA evidence.

Незалежний фінансовий вектор: старт 150 USD; купівля 1 за 103 з комісією 0.103;
продаж 1 за 90 з комісією 0.09. До settlement: account cash 136.807;
settled cash 150; receivable 89.91; payable 103.103; available cash 46.897;
realized P&L −13; fees 0.193; net P&L −13.193. Вектор перевіряється повторно
при precision=2 і ввімкнених Inexact/Rounded traps.

Для partial fills незалежний вектор: старт 500 USD; buy 2 за 103 й sell 2
за 90, кожний як 1 + 1. Після першої buy-частини cash 396.897, position 1,
remaining reserve 103.103, OMS PARTIALLY_FILLED. Після всіх чотирьох fills
cash 473.614, position 0, payable 206.206, receivable 179.82, available 293.794,
realized P&L −26, fees 0.386, net P&L −26.386. Справжній process exit
після першої buy або sell частини відновлює другу без resend/admission.
Фінансовий звіт дорівнює запуску з одним повним fill того самого обсягу.
Повний partial loop також проходить при precision=2 та Inexact/Rounded traps.
Втрата, перестановка чи дублювання retained fill відхиляються без мутації.

Команди кваліфікації:

```text
python tools/baseline.py check
python tools/build_provenance_manifest.py --check
python tools/check_nvda_qualification.py --check-status
PYTHONPATH=research:. python -m unittest discover -s mvp/tests -v
PYTHONPATH=research:. python -m unittest discover -s research/tests -v
dotnet run --project tests/Contracts.DotNet/Contracts.DotNet.csproj --configuration Release -- contracts/fixtures/common-scalars.corpus.json
```

Для незалежних паралельних suite застосовуються окремі TMPDIR; canonical
`tools/verify.py` виконує suite послідовно. ArtifactStore координує parent
namespace, тому одночасні suite зі спільним `/tmp` можуть коректно отримати busy.
Temp fixtures для повного suite мають бути поза Git checkout, наприклад окремий
каталог у `/tmp`: packaged-trust tests правильно відхиляють встановлений runtime
усередині source checkout. Помилка вибору test TMPDIR не виправляється
послабленням production trust boundary.

Точні завершені результати й head SHA фіксуються в описі інтеграційного PR.
Queued/pending CI не означає PASS. Після merge необхідний повторний прогін
на прийнятому main SHA; без нього main acceptance не встановлюється.

## Межі приймання

Це інтеграція існуючого synthetic cash-equity ZERO-циклу, а не закриття всіх
можливих інструментів і розділів 5–9. Partial-fill profile має рівно дві рівні
частини в одному causally available batch; довільний latency/volume scheduler,
ордери між епізодами, кілька валют, funding/borrow/corporate events, ingest реальної історичної бази
та її revision stream потребує окремих composition acceptance gates.
Наявні domain tests таких компонентів не підміняють ці gates.

`HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; native Windows acceptance не
встановлено. Provider/PAPER/LIVE authority не розширюється. Біржові ключі,
credentials, приватні журнали, generated runtime keys і великі артефакти
не включені. Статус economic edge залишається `INCONCLUSIVE`.

Rollback: інтеграційний commit/merge можна revert без видалення попередніх гілок.
Заморожені старі simulation sessions відхиляють змінений source/protocol;
їхній журнал не конвертується неявно й не перезаписується.
