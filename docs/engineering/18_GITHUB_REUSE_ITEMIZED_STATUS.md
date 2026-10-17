# AutoTrade — поіменний звіт про 60 GitHub-джерел

Перевірка: 17 жовтня 2026. Зміни у [PR #2393](https://github.com/Oleksii-debug/AutoTrade/pull/2393), НЕ у main.

## Підсумок

- Джерел у реєстрі: **60**.
- Оригінальні опціональні адаптери для окремих бібліотек: **3** (CCXT, River, Optuna).
- Репозиторії, що залишаються без перенесеного коду: **57**.
- Повних сторонніх дерев коду скопійовано: **0**.
- Ізольовані локальні тести адаптерів і CLI: **7/7 PASS**, із підміною зовнішніх бібліотек. Реальна мережа, пакети та production CI ще не підтверджені.
- Активних сторонніх торгових рушіїв у цьому PR: **0**.
- Жоден upstream-проєкт не оголошено прибутковим чи готовим для live trading.

## Умови використання

MIT/BSD/Apache зазвичай дозволяють приватні зміни, із збереженням належних повідомлень та інших умов. GPL/LGPL дозволяють внутрішнє використання, але при розповсюдженні можуть виникнути вимоги щодо вихідних кодів. AGPL додає окремі мережеві умови. Commons Clause обмежує продаж. Якщо явно наданої ліцензії не встановлено, публічний GitHub не надає автоматично права копіювати. Ліцензії залежностей, закритих плагінів, моделей та ринкових даних перевіряються окремо.

## Усі джерела

### Агенти й координація

| GitHub | Ліцензійні умови | Статус | Відкрите завдання |
|---|---|---|---|
| [hummingbot/condor](https://github.com/hummingbot/condor) | MIT | NOT_IMPORTED | Кваліфікувати рольовий DAG, пам'ять, вартість і політику моделі |
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) | Apache-2.0 | NOT_IMPORTED | Кваліфікувати рольовий DAG, пам'ять, вартість і політику моделі |
| [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) | MIT | NOT_IMPORTED | Кваліфікувати рольовий DAG, пам'ять, вартість і політику моделі |
| [TauricResearch/Trading-R1](https://github.com/TauricResearch/Trading-R1) | NO_CODE_NO_LICENSE | NOT_IMPORTED | Репозиторій порожній — немає що інтегрувати |

### Торгові рушії й боти

| GitHub | Ліцензійні умови | Статус | Відкрите завдання |
|---|---|---|---|
| [QuantConnect/Lean](https://github.com/QuantConnect/Lean) | Apache-2.0 | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) | Apache-2.0 | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [AI4Finance-Foundation/FinRL-Trading](https://github.com/AI4Finance-Foundation/FinRL-Trading) | Apache-2.0 | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [Superalgos/Superalgos](https://github.com/Superalgos/Superalgos) | Apache-2.0 | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [jesse-ai/jesse](https://github.com/jesse-ai/jesse) | MIT | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) | GPL-3.0 | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [Drakkar-Software/OctoBot](https://github.com/Drakkar-Software/OctoBot) | GPL-3.0 | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [mementum/backtrader](https://github.com/mementum/backtrader) | GPL-3.0 | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [kernc/backtesting.py](https://github.com/kernc/backtesting.py) | AGPL-3.0 | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | LGPL-3.0 | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [polakowo/vectorbt](https://github.com/polakowo/vectorbt) | Apache-2.0 + Commons Clause | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [vnpy/vnpy](https://github.com/vnpy/vnpy) | MIT | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [pmorissette/bt](https://github.com/pmorissette/bt) | MIT | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [stefan-jansen/zipline-reloaded](https://github.com/stefan-jansen/zipline-reloaded) | Apache-2.0 | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [quantopian/zipline](https://github.com/quantopian/zipline) | Apache-2.0 | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [ta4j/ta4j](https://github.com/ta4j/ta4j) | MIT (source header; root license not located) | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |
| [freqtrade/freqtrade-strategies](https://github.com/freqtrade/freqtrade-strategies) | GPL-3.0 | NOT_IMPORTED | Дослідити вузькі стратегії без дублювання OMS/ledger |

### Машинне навчання й дослідження

| GitHub | Ліцензійні умови | Статус | Відкрите завдання |
|---|---|---|---|
| [microsoft/qlib](https://github.com/microsoft/qlib) | MIT | NOT_IMPORTED | Перевірити causal labels, тренування, holdout і model promotion |
| [online-ml/river](https://github.com/online-ml/river) | BSD-3-Clause | BRIDGE_CODE_IN_PR | Виконати реальні package/CI/causality тести та прив'язку до чинного агента |
| [optuna/optuna](https://github.com/optuna/optuna) | MIT | BRIDGE_CODE_IN_PR | Виконати реальні package/CI/causality тести та прив'язку до чинного агента |
| [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL) | MIT | NOT_IMPORTED | Перевірити causal labels, тренування, holdout і model promotion |
| [AI4Finance-Foundation/FinRL_Crypto](https://github.com/AI4Finance-Foundation/FinRL_Crypto) | MIT | NOT_IMPORTED | Перевірити causal labels, тренування, holdout і model promotion |
| [AI4Finance-Foundation/FinRL-Meta](https://github.com/AI4Finance-Foundation/FinRL-Meta) | MIT | NOT_IMPORTED | Перевірити causal labels, тренування, holdout і model promotion |
| [scikit-learn/scikit-learn](https://github.com/scikit-learn/scikit-learn) | BSD-3-Clause | NOT_IMPORTED | Перевірити causal labels, тренування, holdout і model promotion |
| [scipy/scipy](https://github.com/scipy/scipy) | BSD-style + third-party notices | NOT_IMPORTED | Перевірити causal labels, тренування, holdout і model promotion |
| [cvxpy/cvxpy](https://github.com/cvxpy/cvxpy) | Apache-2.0 | NOT_IMPORTED | Перевірити causal labels, тренування, holdout і model promotion |
| [pytorch/pytorch](https://github.com/pytorch/pytorch) | BSD-style + third-party notices | NOT_IMPORTED | Перевірити causal labels, тренування, holdout і model promotion |
| [microsoft/onnxruntime](https://github.com/microsoft/onnxruntime) | MIT | NOT_IMPORTED | Перевірити causal labels, тренування, holdout і model promotion |
| [huggingface/safetensors](https://github.com/huggingface/safetensors) | NOT_RECHECKED_GITHUB_SOURCE_UNAVAILABLE | NOT_IMPORTED | Знайти чинний файл ліцензії; без цього не копіювати |
| [lballabio/QuantLib](https://github.com/lballabio/QuantLib) | BSD-style + retained notices | NOT_IMPORTED | Перевірити causal labels, тренування, holdout і model promotion |
| [mlflow/mlflow](https://github.com/mlflow/mlflow) | Apache-2.0 | NOT_IMPORTED | Перевірити causal labels, тренування, holdout і model promotion |

### Біржові API й конектори

| GitHub | Ліцензійні умови | Статус | Відкрите завдання |
|---|---|---|---|
| [ccxt/ccxt](https://github.com/ccxt/ccxt) | MIT | BRIDGE_CODE_IN_PR | Виконати реальні package/CI/causality тести та прив'язку до чинного агента |
| [JKorf/CryptoExchange.Net](https://github.com/JKorf/CryptoExchange.Net) | MIT | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |
| [JKorf/WhiteBit.Net](https://github.com/JKorf/WhiteBit.Net) | MIT | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |
| [alpacahq/alpaca-trade-api-csharp](https://github.com/alpacahq/alpaca-trade-api-csharp) | Apache-2.0 | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |
| [QuantConnect/Lean.Brokerages.Bybit](https://github.com/QuantConnect/Lean.Brokerages.Bybit) | NO_ROOT_LICENSE_VERIFIED | NOT_IMPORTED | Отримати явний дозвіл або достатнє підтвердження ліцензійних умов |
| [QuantConnect/Lean.Brokerages.Alpaca](https://github.com/QuantConnect/Lean.Brokerages.Alpaca) | NO_ROOT_LICENSE_VERIFIED | NOT_IMPORTED | Отримати явний дозвіл або достатнє підтвердження ліцензійних умов |
| [QuantConnect/Lean.Brokerages.InteractiveBrokers](https://github.com/QuantConnect/Lean.Brokerages.InteractiveBrokers) | Apache-2.0 | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |
| [QuantConnect/Lean.Brokerages.Binance](https://github.com/QuantConnect/Lean.Brokerages.Binance) | Apache-2.0 | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |
| [QuantConnect/Lean.Brokerages.Kraken](https://github.com/QuantConnect/Lean.Brokerages.Kraken) | Apache-2.0 | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |
| [QuantConnect/Lean.Brokerages.Coinbase](https://github.com/QuantConnect/Lean.Brokerages.Coinbase) | Apache-2.0 | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |
| [JKorf/Binance.Net](https://github.com/JKorf/Binance.Net) | MIT | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |
| [JKorf/Bybit.Net](https://github.com/JKorf/Bybit.Net) | MIT | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |
| [JKorf/OKX.Net](https://github.com/JKorf/OKX.Net) | MIT | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |
| [JKorf/Kraken.Net](https://github.com/JKorf/Kraken.Net) | MIT | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |
| [binance/binance-connector-python](https://github.com/binance/binance-connector-python) | MIT | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |
| [hummingbot/gateway](https://github.com/hummingbot/gateway) | Apache-2.0 | NOT_IMPORTED | Кваліфікувати API, маршрути, запити, reconciliation та права |

### Інфраструктура, дані й UI

| GitHub | Ліцензійні умови | Статус | Відкрите завдання |
|---|---|---|---|
| [apache/arrow](https://github.com/apache/arrow) | Apache-2.0 | NOT_IMPORTED | Обрати точну версію й підтвердити доцільність, безпеку та сумісність |
| [duckdb/duckdb](https://github.com/duckdb/duckdb) | MIT | NOT_IMPORTED | Обрати точну версію й підтвердити доцільність, безпеку та сумісність |
| [OpenBB-finance/OpenBB](https://github.com/OpenBB-finance/OpenBB) | NOT_RECHECKED_GITHUB_SOURCE_UNAVAILABLE | NOT_IMPORTED | Знайти чинний файл ліцензії; без цього не копіювати |
| [hummingbot/dashboard](https://github.com/hummingbot/dashboard) | Apache-2.0 | NOT_IMPORTED | Обрати точну версію й підтвердити доцільність, безпеку та сумісність |
| [dotnet/aspnetcore](https://github.com/dotnet/aspnetcore) | MIT | NOT_IMPORTED | Обрати точну версію й підтвердити доцільність, безпеку та сумісність |
| [dotnet/wpf](https://github.com/dotnet/wpf) | MIT | NOT_IMPORTED | Обрати точну версію й підтвердити доцільність, безпеку та сумісність |
| [facebook/react](https://github.com/facebook/react) | NOT_RECHECKED_GITHUB_SOURCE_UNAVAILABLE | NOT_IMPORTED | Знайти чинний файл ліцензії; без цього не копіювати |
| [open-telemetry/opentelemetry-dotnet](https://github.com/open-telemetry/opentelemetry-dotnet) | Apache-2.0 | NOT_IMPORTED | Обрати точну версію й підтвердити доцільність, безпеку та сумісність |
| [microsoft/playwright](https://github.com/microsoft/playwright) | Apache-2.0 | NOT_IMPORTED | Обрати точну версію й підтвердити доцільність, безпеку та сумісність |

Повний machine-readable реєстр та детальні причини: [control/external_reuse/catalog.json](../../control/external_reuse/catalog.json).
