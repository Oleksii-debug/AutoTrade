# AutoTrade — цільовий організм і планувальник уваги

Дата: 2026-10-18  
Статус: **PHASE 1 IMPLEMENTED / ZERO RESEARCH ONLY**

## Рішення

AutoTrade залишається однією програмою й одним фінансовим організмом. Різні
цілі не отримують власної бухгалтерії, ризик-контролю, OMS або незалежного
права витрачати той самий капітал. Вони є workstream-контекстами одного
координатора:

- один market-data snapshot кешується і повторно використовується;
- портфель, резерви, ризик, бухгалтерія, OMS та незалежний критик спільні;
- пам'ять, модель, гіпотеза й результати кожного workstream ізольовані;
- один organism-wide scheduler розподіляє глибоку увагу й платні model calls;
- усі шість профілів стартують із нульовим платним бюджетом; сама ціль
  25/50/100% ніколи не є дозволом витрачати API-кредити;
- пасивні workstreams не викликають LLM, доки ціна, час, ризик, manual wake або
  проблема даних не створять подію;
- target-return змінює функцію оцінювання й пошук можливостей, але не підвищує
  hard risk, leverage або trading authority.

## Шість режимів уваги

| Profile | Ціль | Горизонт | Типова увага |
| --- | ---: | ---: | --- |
| BASELINE | без нав'язаної дохідності | rolling | звичайний контроль |
| TARGET_25 | +25% net | до 24 годин | активний intraday research |
| TARGET_50 | +50% net | до 24 годин | активний high-opportunity research |
| TARGET_100 | +100% net | до 24 годин | найжорсткіший експериментальний target |
| EVENT_DRIVEN_POSITION | thesis/position | години–тижні | сон + deterministic triggers |
| OPPORTUNITY_RESEARCH | launchpad/reward/new listing | подієвий | research-only, без ордерів |

TARGET_25/50/100 — цілі експерименту, не обіцянки. Результат може бути
TARGET_MET, TARGET_MISSED, RISK_LIMIT_BREACHED або INCONCLUSIVE.
Режим не має права "добивати" ціль збільшенням ставки після збитку.

## Активна короткострокова лабораторія

Перші сімейства для причинного порівняння:

1. cost-aware momentum;
2. volatility/opening-range breakout;
3. mean reversion;
4. order-book imbalance і microprice;
5. relative-value/pairs;
6. liquidity-aware passive quoting як окремий майбутній експеримент;
7. Fibonacci retracement/extension лише як ознака або trigger, а не як
   самодостатній доказ переваги.

Кожен кандидат порівнюється на одному causal cut із NO_TRADE, cash,
buy-and-hold і чинним champion. Метрики: net return after fees/spread/slippage,
час до target, частка target hits, max drawdown, turnover, missed fills,
ліквідність, tail loss, ймовірність ruin та стабільність між режимами ринку.
Параметри freeze до holdout; agent не може сам себе проголосити champion.

## Тихий event-driven режим

EVENT_DRIVEN_POSITION не тримає LLM активною годинами:

1. глибокий аналіз формує thesis, reference price, take-profit, stop-loss,
   invalidation і scheduled review;
2. zero-model monitor читає спільний market stream;
3. без trigger workstream залишається PASSIVE_MONITORING;
4. threshold, deadline, manual wake або anomaly переводить його у
   WAKE_ANALYSIS;
5. stale/reordered/future data переводить у RECOVERY без платного model call;
6. глобальний scheduler дає глибоку/платну увагу лише найтерміновішому
   workstream у межах бюджету.

Цей механізм дозволяє одночасно вести швидкі intraday гіпотези й довші
позиційні гіпотези без множення процесів, LLM-контекстів і витрат.

## Нові токени, винагороди та launchpad

Окремий OPPORTUNITY_RESEARCH збирає умови Launchpad, нових listings,
vesting, WBT holding, reward/airdrop/retrodrop і ризики smart-contract/project.
Він research-only: участь може вимагати капіталу, holding, голосування,
vesting, KYC або ручної дії; "безкоштовний токен" не вважається безризиковим
прибутком. Усі умови та часові дедлайни мають versioned evidence.

Офіційна WhiteBIT документація підтверджує:

- public prices/orderbooks/trades доступні без API key;
- WebSocket придатний для постійного дешевого monitor;
- публічного testnet/sandbox немає;
- spot, margin і perpetual futures є різними capabilities;
- futures leverage не є дозволом AutoTrade на leverage;
- kill-switch може скасувати відкриті orders, якщо клієнт перестав оновлювати
  timer;
- Launchpad може містити fixed-price allocation, refund/FCFS/extended
  mechanics і vesting, які треба читати окремо для кожного проєкту.

## Дослідницьке обґрунтування

- Дослідження BTC/USDT 2018–2026 показало: naive hourly ML-сигнали можуть
  зникати після 10 bps costs, тоді як cost-aware execution filter різко
  зменшив turnover; у наведеній конфігурації trades скоротилися з 10,619 до
  251. Отже, головний важіль — не нескінченне "мислення" LLM, а відсікання
  слабких сигналів до виконання.
- Велике дослідження limit-order books знаходить короткострокову
  передбачуваність, але результат сильно залежить від representation; order
  flow/volume кращі за прості level snapshots. Це обґрунтовує централізований
  order-book feature service.
- Емпіричні day-trader дані показують великий розкид навичок і persistence у
  невеликої верхньої групи; тому потрібні holdout, retention і independent
  critic, а не припущення, що агресивна ціль автоматично створить edge.
- Попередні high-frequency роботи показують високу чутливість до costs і
  execution delay. Тому target score рахується лише після fees, spread,
  slippage та missed fills.

Джерела:

- https://docs.whitebit.com/
- https://docs.whitebit.com/llms.txt
- https://help.whitebit.com/hc/en-gb/articles/13129367706525-WhiteBIT-Launchpad
- https://arxiv.org/abs/2606.00060
- https://arxiv.org/abs/2211.13777
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=529063
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1611623

## Реалізація Phase 1

- research/autotrade_research/agents/goal_directed.py:
  canonical profiles, isolated memory namespaces, shared-authority manifest,
  deterministic wake decisions, organism-wide attention allocation й
  after-cost target evaluation.
- tools/create_goal_directed_organism.py:
  atomic persistent manifest і шість memories поза updateable app directory.
- tools/run_goal_directed_attention.py:
  постійний zero-model цикл над одним WhiteBIT snapshot, перевірка цілісності
  manifest, event wake та атомарне збереження attention state без ордерів.
- research/tests/test_goal_directed_organism.py:
  target/risk/memory/wake/budget/causality/persistence tests.

Наступний зріз: під'єднати contracts до historical candidate matrix, запустити
однакові BTC/ETH causal cuts для baseline/target-conditioned candidate
selection, потім під'єднати WhiteBIT order-book features до event-driven
scheduler. До позитивного незалежного результату всі режими залишаються
ZERO/SIMULATION.
