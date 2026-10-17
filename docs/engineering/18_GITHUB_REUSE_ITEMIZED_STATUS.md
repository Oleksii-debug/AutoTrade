# AutoTrade — поіменний звіт про 60 GitHub-джерел

Створено: 2026-10-17. Стан: робоча гілка та draft PR #2393, НЕ злитий у main.

## Фактичні цифри

- Занесено до реєстру: **60 GitHub-репозиторіїв**.
- Написано три окремі опціональні дослідницькі адаптери до бібліотек CCXT, River та Optuna; upstream-дерева не скопійовані.
- Зберігаються без інтегрованого коду: **57 джерел**.
- Усі 60 зовнішніх репозиторіїв цілком скопійовані: **0**.
- Реальна торговельна активність, інтеграція в Plan10 runtime та реліз: **не активовано**.
- Ізольовані локальні негативні та позитивні тести: **6/6 PASS** з підміною зовнішніх пакетів; реальне API та upstream runtime не перевірені.

## Важливі правові групи

MIT, BSD, Apache: допускають приватні модифікації, але мають умови збереження повідомлень/прав. GPL/LGPL: приватне використання загалом допускається, публікація потребує дотримання copyleft-умов. AGPL: додаткові мережеві обов’язки. Commons Clause: обмеження продажу. Без явного дозволу код копіювати не можна. Окремі пакети/дані/плагіни можуть мати інші умови.

## Мультиагентні системи й оркестрація

| Джерело | Ліцензія / стан перевірки | Новий стан | Що потрібно |
|---|---|---|---|
| [hummingbot/condor](https://github.com/hummingbot/condor) | MIT | NOT_IMPORTED | Продовжити аналіз контрактів і пам’яті агентів; не створювати другу торгову владу. |
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) | Apache-2.0 | NOT_IMPORTED | Продовжити аналіз контрактів і пам’яті агентів; не створювати другу торгову владу. |
| [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent) | MIT | NOT_IMPORTED | Продовжити аналіз контрактів і пам’яті агентів; не створювати другу торгову владу. |
| [TauricResearch/Trading-R1](https://github.com/TauricResearch/Trading-R1) | NO_CODE_NO_LICENSE | NOT_IMPORTED | Немає доступного коду |

## Готові торгові рушії, стратегії й бек-тестери

| Джерело | Ліцензія / стан перевірки | Новий стан | Що потрібно |
|---|---|---|---|
| [QuantConnect/Lean](https://github.com/QuantConnect/Lean) | Apache-2.0 | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) | Apache-2.0 | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [AI4Finance-Foundation/FinRL-Trading](https://github.com/AI4Finance-Foundation/FinRL-Trading) | Apache-2.0 | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [Superalgos/Superalgos](https://github.com/Superalgos/Superalgos) | Apache-2.0 | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [jesse-ai/jesse](https://github.com/jesse-ai/jesse) | MIT | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) | GPL-3.0 | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [Drakkar-Software/OctoBot](https://github.com/Drakkar-Software/OctoBot) | GPL-3.0 | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [mementum/backtrader](https://github.com/mementum/backtrader) | GPL-3.0 | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [kernc/backtesting.py](https://github.com/kernc/backtesting.py) | AGPL-3.0 | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | LGPL-3.0 | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [polakowo/vectorbt](https://github.com/polakowo/vectorbt) | Apache-2.0 + Commons Clause | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [vnpy/vnpy](https://github.com/vnpy/vnpy) | MIT | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [pmorissette/bt](https://github.com/pmorissette/bt) | NOT_RECHECKED | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [stefan-jansen/zipline-reloaded](https://github.com/stefan-jansen/zipline-reloaded) | NOT_RECHECKED | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [quantopian/zipline](https://github.com/quantopian/zipline) | NOT_RECHECKED | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [ta4j/ta4j](https://github.com/ta4j/ta4j) | NOT_RECHECKED | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |
| [freqtrade/freqtrade-strategies](https://github.com/freqtrade/freqtrade-strategies) | NOT_RECHECKED | NOT_IMPORTED | Потрібні вузькі адаптери стратегій і порівняльні тести; повний імпорт дублює ядро AutoTrade. |

## Математичні та навчальні бібліотеки

| Джерело | Ліцензія / стан перевірки | Новий стан | Що потрібно |
|---|---|---|---|
| [microsoft/qlib](https://github.com/microsoft/qlib) | MIT | NOT_IMPORTED | Необхідні окремі дослідницькі інтеграції, залежності, holdout та тести причинності. |
| [online-ml/river](https://github.com/online-ml/river) | BSD-3-Clause | BRIDGE_CODE_IN_PR | Адаптер у PR; потрібні справжні пакети, CI, кваліфікація |
| [optuna/optuna](https://github.com/optuna/optuna) | MIT | BRIDGE_CODE_IN_PR | Адаптер у PR; потрібні справжні пакети, CI, кваліфікація |
| [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL) | MIT | NOT_IMPORTED | Необхідні окремі дослідницькі інтеграції, залежності, holdout та тести причинності. |
| [AI4Finance-Foundation/FinRL_Crypto](https://github.com/AI4Finance-Foundation/FinRL_Crypto) | MIT | NOT_IMPORTED | Необхідні окремі дослідницькі інтеграції, залежності, holdout та тести причинності. |
| [AI4Finance-Foundation/FinRL-Meta](https://github.com/AI4Finance-Foundation/FinRL-Meta) | NOT_RECHECKED | NOT_IMPORTED | Необхідні окремі дослідницькі інтеграції, залежності, holdout та тести причинності. |
| [scikit-learn/scikit-learn](https://github.com/scikit-learn/scikit-learn) | BSD-3-Clause (historical shortlist) | NOT_IMPORTED | Необхідні окремі дослідницькі інтеграції, залежності, holdout та тести причинності. |
| [scipy/scipy](https://github.com/scipy/scipy) | NOT_RECHECKED | NOT_IMPORTED | Необхідні окремі дослідницькі інтеграції, залежності, holdout та тести причинності. |
| [cvxpy/cvxpy](https://github.com/cvxpy/cvxpy) | NOT_RECHECKED | NOT_IMPORTED | Необхідні окремі дослідницькі інтеграції, залежності, holdout та тести причинності. |
| [pytorch/pytorch](https://github.com/pytorch/pytorch) | NOT_RECHECKED | NOT_IMPORTED | Необхідні окремі дослідницькі інтеграції, залежності, holdout та тести причинності. |
| [microsoft/onnxruntime](https://github.com/microsoft/onnxruntime) | NOT_RECHECKED | NOT_IMPORTED | Необхідні окремі дослідницькі інтеграції, залежності, holdout та тести причинності. |
| [huggingface/safetensors](https://github.com/huggingface/safetensors) | NOT_RECHECKED | NOT_IMPORTED | Необхідні окремі дослідницькі інтеграції, залежності, holdout та тести причинності. |
| [lballabio/QuantLib](https://github.com/lballabio/QuantLib) | BSD-style (historical file) | NOT_IMPORTED | Необхідні окремі дослідницькі інтеграції, залежності, holdout та тести причинності. |
| [mlflow/mlflow](https://github.com/mlflow/mlflow) | NOT_RECHECKED | NOT_IMPORTED | Необхідні окремі дослідницькі інтеграції, залежності, holdout та тести причинності. |

## Біржі, провайдери, SDK і конектори

| Джерело | Ліцензія / стан перевірки | Новий стан | Що потрібно |
|---|---|---|---|
| [ccxt/ccxt](https://github.com/ccxt/ccxt) | MIT | BRIDGE_CODE_IN_PR | Адаптер у PR; потрібні справжні пакети, CI, кваліфікація |
| [JKorf/CryptoExchange.Net](https://github.com/JKorf/CryptoExchange.Net) | MIT | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |
| [JKorf/WhiteBit.Net](https://github.com/JKorf/WhiteBit.Net) | MIT | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |
| [alpacahq/alpaca-trade-api-csharp](https://github.com/alpacahq/alpaca-trade-api-csharp) | Apache-2.0 | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |
| [QuantConnect/Lean.Brokerages.Bybit](https://github.com/QuantConnect/Lean.Brokerages.Bybit) | NO_ROOT_LICENSE_VERIFIED | NOT_IMPORTED | Знайти письмовий дозвіл або файл ліцензії |
| [QuantConnect/Lean.Brokerages.Alpaca](https://github.com/QuantConnect/Lean.Brokerages.Alpaca) | NO_ROOT_LICENSE_VERIFIED | NOT_IMPORTED | Знайти письмовий дозвіл або файл ліцензії |
| [QuantConnect/Lean.Brokerages.InteractiveBrokers](https://github.com/QuantConnect/Lean.Brokerages.InteractiveBrokers) | Apache-2.0 (historical metadata) | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |
| [QuantConnect/Lean.Brokerages.Binance](https://github.com/QuantConnect/Lean.Brokerages.Binance) | Apache-2.0 (historical metadata) | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |
| [QuantConnect/Lean.Brokerages.Kraken](https://github.com/QuantConnect/Lean.Brokerages.Kraken) | Apache-2.0 (historical metadata) | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |
| [QuantConnect/Lean.Brokerages.Coinbase](https://github.com/QuantConnect/Lean.Brokerages.Coinbase) | NOT_RECHECKED | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |
| [JKorf/Binance.Net](https://github.com/JKorf/Binance.Net) | NOT_RECHECKED | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |
| [JKorf/Bybit.Net](https://github.com/JKorf/Bybit.Net) | NOT_RECHECKED | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |
| [JKorf/OKX.Net](https://github.com/JKorf/OKX.Net) | NOT_RECHECKED | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |
| [JKorf/Kraken.Net](https://github.com/JKorf/Kraken.Net) | NOT_RECHECKED | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |
| [binance/binance-connector-python](https://github.com/binance/binance-connector-python) | NOT_RECHECKED | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |
| [hummingbot/gateway](https://github.com/hummingbot/gateway) | NOT_RECHECKED | NOT_IMPORTED | Потрібна перевірка SDK/API, контрактів, ліцензій, ключів і розмежування виконання. |

## Дані, UI, інфраструктура й інструменти

| Джерело | Ліцензія / стан перевірки | Новий стан | Що потрібно |
|---|---|---|---|
| [apache/arrow](https://github.com/apache/arrow) | NOT_RECHECKED | NOT_IMPORTED | Потрібні обґрунтування потреби, версії, ліцензійні перевірки та кросплатформні тести. |
| [duckdb/duckdb](https://github.com/duckdb/duckdb) | NOT_RECHECKED | NOT_IMPORTED | Потрібні обґрунтування потреби, версії, ліцензійні перевірки та кросплатформні тести. |
| [OpenBB-finance/OpenBB](https://github.com/OpenBB-finance/OpenBB) | NOT_RECHECKED | NOT_IMPORTED | Потрібні обґрунтування потреби, версії, ліцензійні перевірки та кросплатформні тести. |
| [hummingbot/dashboard](https://github.com/hummingbot/dashboard) | NOT_RECHECKED | NOT_IMPORTED | Потрібні обґрунтування потреби, версії, ліцензійні перевірки та кросплатформні тести. |
| [dotnet/aspnetcore](https://github.com/dotnet/aspnetcore) | NOT_RECHECKED | NOT_IMPORTED | Потрібні обґрунтування потреби, версії, ліцензійні перевірки та кросплатформні тести. |
| [dotnet/wpf](https://github.com/dotnet/wpf) | NOT_RECHECKED | NOT_IMPORTED | Потрібні обґрунтування потреби, версії, ліцензійні перевірки та кросплатформні тести. |
| [facebook/react](https://github.com/facebook/react) | NOT_RECHECKED | NOT_IMPORTED | Потрібні обґрунтування потреби, версії, ліцензійні перевірки та кросплатформні тести. |
| [open-telemetry/opentelemetry-dotnet](https://github.com/open-telemetry/opentelemetry-dotnet) | NOT_RECHECKED | NOT_IMPORTED | Потрібні обґрунтування потреби, версії, ліцензійні перевірки та кросплатформні тести. |
| [microsoft/playwright](https://github.com/microsoft/playwright) | NOT_RECHECKED | NOT_IMPORTED | Потрібні обґрунтування потреби, версії, ліцензійні перевірки та кросплатформні тести. |

## Технічне правило прийняття

Сам факт MIT/GPL/іншої ліцензії не є підставою імпортувати цілу систему. Для кожного джерела потрібен точний source SHA, підтверджені права, запланована інтеграційна точка, збірка, тести, безпечне злиття й exact-main readback. Не створювати другу бухгалтерію, OMS або ризикову владу. Заявки реальними грошима не вмикати.
