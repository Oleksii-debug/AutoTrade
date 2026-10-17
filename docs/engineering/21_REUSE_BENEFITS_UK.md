# Навіщо AutoTrade кожне зі сторонніх джерел

17.10.2026. Коротко українською: що дає компонент і що довелося б робити без нього.

## Не плутати перелік із виконаною інтеграцією

У попередній хвилі написані лише три опціональні дослідницькі адаптери: CCXT, River, Optuna. Вони знаходяться у PR #2393, не є завершеною інтеграцією всіх upstream-програм у кінцевий продукт. Решта 57 джерел — кандидати. Нижче описано потенційну користь, а не твердження, що всі ці можливості вже працюють.

Новий WhiteBIT public collector — власний код AutoTrade, не четвертий скопійований GitHub-репозиторій. Він дає production-public REST дані без ключа; симуляційне виконання залишається нашим. Наскрізне Host/UI/ArtifactStore підключення ще не кваліфіковане.

Відсутність окремої бібліотеки не обов'язково означає відсутність функції: її може давати вже наявний модуль, інша бібліотека або наша реалізація. Більше залежностей не означає кращу торгову систему.

## Агенти та координація — 4 джерела

| Джерело | Що корисного | Без нього / важлива межа |
|---|---|---|
| [Condor](https://github.com/hummingbot/condor) | Організація роботи кількох агентів, їхніх стратегій і журналів | Більше власної оркестрації; не заміна нашого ризику й журналу |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | Група аналітиків, аргументи за/проти та координація висновків | Дискусійний протокол потрібно робити самим; не доказ прибутковості |
| [RD-Agent](https://github.com/microsoft/RD-Agent) | Автоматизація дослідницьких гіпотез та експериментів | Більше ручної організації наукового циклу; код дослідів ізолювати |
| [Trading-R1](https://github.com/TauricResearch/Trading-R1) | Потенційне джерело фінансового reasoning, якщо код опублікують | За попередньою перевіркою коду не було; поточна користь від інтеграції не доведена |

## Торгові рушії та стратегії — 17 джерел

| Джерело | Що корисного | Без нього / важлива межа |
|---|---|---|
| [LEAN](https://github.com/QuantConnect/Lean) | Обране багатокласове фінансове ядро, інструменти й ордери | Найбільша потенційна економія розробки; потрібне реальне embedding-приймання |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | Маркетмейкінг, біржові з'єднання, приклади paper-ботів | Стратегії та транспорт доведеться реалізовувати/перевіряти окремо |
| [FinRL-X](https://github.com/AI4Finance-Foundation/FinRL-Trading) | Єдина мова цільових ваг портфеля для різних стратегій | Потрібен власний контракт поєднання математичних/навчених сигналів |
| [Superalgos](https://github.com/Superalgos/Superalgos) | Ідеї керування процесами збору даних і ботами | Власне керування процесами; візуальні схеми не дорівнюють NVDA-доступності |
| [Jesse](https://github.com/jesse-ai/jesse) | Дослідницькі стратегії, аналіз, оптимізація та ML | Більше власних research-інструментів; платні частини окремо |
| [Freqtrade/FreqAI](https://github.com/freqtrade/freqtrade) | Цикли криптостратегій та перенавчання прогнозних моделей | Власний навчальний цикл складніший; GPL та окреме доведення економіки |
| [OctoBot](https://github.com/Drakkar-Software/OctoBot) | Модульні grid/DCA боти та конфігурація | Більше власних стратегій; великий імпорт може ускладнити продукт |
| [Backtrader](https://github.com/mementum/backtrader) | Прозорі приклади подієвих стратегій і симуляції | Менше незалежних контрольних реалізацій; не потрібне друге ядро |
| [Backtesting.py](https://github.com/kernc/backtesting.py) | Компактні контрольні досліди | Їх можна реалізувати в наявному рушії; AGPL потребує аналізу поширення |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | Альтернативне високоточне Rust/Python подієве ядро | Без нього немає цього запасного варіанта; не запускати два фінансові ядра |
| [VectorBT](https://github.com/polakowo/vectorbt) | Пакетне порівняння великої кількості параметрів | Можуть бути повільнішими досліди; Commons Clause і PRO окремо |
| [vn.py](https://github.com/vnpy/vnpy) | Приклади шлюзів, подієвих стратегій та робочої станції | Менше готових gateway-патернів; не автоматично сумісний інтерфейс |
| [bt](https://github.com/pmorissette/bt) | Дослідження портфелів і ребалансування | Аналоги можливі в LEAN/наших тестах |
| [Zipline Reloaded](https://github.com/stefan-jansen/zipline-reloaded) | Подієве тестування стратегій акцій | Менше альтернатив для перехресної перевірки; окреме ядро не обов'язкове |
| [Zipline](https://github.com/quantopian/zipline) | Історичне джерело архітектури подієвих дослідів | Не суттєва втрата за наявності сучасної альтернативи; legacy перевіряти |
| [ta4j](https://github.com/ta4j/ta4j) | Приклади технічних індикаторів і правил | У нас можуть бути еквіваленти LEAN/Python; Java-залежність не обов'язкова |
| [Freqtrade strategies](https://github.com/freqtrade/freqtrade-strategies) | Приклади стартових гіпотез для тестування | Потрібні власні гіпотези; приклад не є перевіреною прибутковою стратегією |

## Навчання та кількісні дослідження — 14 джерел

| Джерело | Що корисного | Без нього / важлива межа |
|---|---|---|
| [Qlib](https://github.com/microsoft/qlib) | Ознаки ринку, оцінювання моделей, кількісний research | Більше власного ML-конвеєра; не фінансовий dispatcher |
| [River](https://github.com/online-ml/river) | Поступове навчання на нових завершених даних | Власні online-оцінювачі; наш адаптер поки лише дослідницький |
| [Optuna](https://github.com/optuna/optuna) | Автоматичний пошук параметрів з бюджетом спроб | Ручний/grid-пошук; оптимізація без holdout може лише перенавчити |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | Приклади навчання торгових політик з підкріпленням | Власні RL-середовища; складність мусить довести користь |
| [FinRL Crypto](https://github.com/AI4Finance-Foundation/FinRL_Crypto) | Криптоспецифічні RL-досліди | Більше адаптації даних/винагороди; не готовий прибутковий агент |
| [FinRL Meta](https://github.com/AI4Finance-Foundation/FinRL-Meta) | Дослідницькі середовища й робота з даними | Більше власних навчальних оточень; не заміна канонічного replay |
| [scikit-learn](https://github.com/scikit-learn/scikit-learn) | Звичайні прогнозні моделі й оцінювання | Власні estimators або інша бібліотека; практична ML-основа |
| [SciPy](https://github.com/scipy/scipy) | Статистика, оптимізація, наукові обчислення | Потрібні інші числові реалізації; гроші все одно точними decimals |
| [CVXPY](https://github.com/cvxpy/cvxpy) | Формальне розв'язання задач розподілу портфеля | Простішi/власні оптимізатори; не джерело торгових прав |
| [PyTorch](https://github.com/pytorch/pytorch) | Тренування нейромереж | Немає цього deep-learning стека; для першого агента не обов'язковий |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | Перенесення навчених моделей між середовищами виконання | Важча залежність від тренувального стека; потрібна перевірка еквівалентності |
| [Safetensors](https://github.com/huggingface/safetensors) | Формат ваг без виконання Python pickle при читанні | Потрібен інший безпечний формат; це не гарантія безпечності моделі |
| [QuantLib](https://github.com/lballabio/QuantLib) | Незалежні оцінки складних фінансових інструментів | Більше власної математики деривативів; не встановлює маржу біржі |
| [MLflow](https://github.com/mlflow/mlflow) | Порівняння та огляд експериментів | Власний research dashboard; наш registry залишається владою просування |

## Провайдери й SDK — 16 джерел

| Джерело | Що корисного | Без нього / важлива межа |
|---|---|---|
| [CCXT](https://github.com/ccxt/ccxt) | Спільний інтерфейс численних крипто-API | Більше окремих transport-адаптерів; наш міст поки лише public OHLCV |
| [CryptoExchange.Net](https://github.com/JKorf/CryptoExchange.Net) | Спільний .NET HTTP/WS транспорт і API-ліміти | Більше підтримки мережевих деталей |
| [WhiteBit.Net](https://github.com/JKorf/WhiteBit.Net) | Типізований .NET клієнт WhiteBIT | Підтримувати формати самим; не потрібен для нового stdlib public reader |
| [Alpaca C# SDK](https://github.com/alpacahq/alpaca-trade-api-csharp) | Офіційні API-моделі Alpaca | Більше власної підтримки broker API |
| [LEAN Bybit](https://github.com/QuantConnect/Lean.Brokerages.Bybit) | Приклад стику LEAN з Bybit | Потрібна власна/інша інтеграція; права копіювання ще не встановлені |
| [LEAN Alpaca](https://github.com/QuantConnect/Lean.Brokerages.Alpaca) | Приклад стику LEAN з Alpaca | Є SDK-альтернатива; права копіювання ще не встановлені |
| [LEAN IBKR](https://github.com/QuantConnect/Lean.Brokerages.InteractiveBrokers) | Інтеграція брокера традиційних ринків | Більше складної роботи із сесіями брокера; SDK має окремі умови |
| [LEAN Binance](https://github.com/QuantConnect/Lean.Brokerages.Binance) | Приклад Binance brokerage для LEAN | Потрібний альтернативний шлюз і однакова фінансова перевірка |
| [LEAN Kraken](https://github.com/QuantConnect/Lean.Brokerages.Kraken) | Приклад Kraken brokerage | Аналогічний gateway доведеться підтримувати самим |
| [LEAN Coinbase](https://github.com/QuantConnect/Lean.Brokerages.Coinbase) | Приклад Coinbase brokerage | Аналогічний gateway доведеться підтримувати самим |
| [Binance.Net](https://github.com/JKorf/Binance.Net) | Типізований .NET клієнт Binance | Більше власних моделей REST/WS |
| [Bybit.Net](https://github.com/JKorf/Bybit.Net) | Типізований .NET клієнт Bybit | Більше власних моделей REST/WS |
| [OKX.Net](https://github.com/JKorf/OKX.Net) | Типізований .NET клієнт OKX | Більше регіональних/API особливостей підтримувати самим |
| [Kraken.Net](https://github.com/JKorf/Kraken.Net) | Типізований .NET клієнт Kraken | Власний транспорт та форми відповідей |
| [Binance Python connector](https://github.com/binance/binance-connector-python) | Офіційні Python-компоненти API | Інший SDK або власний HTTP/WS клієнт |
| [Hummingbot Gateway](https://github.com/hummingbot/gateway) | Приклади підключення децентралізованих майданчиків | Менше DEX-можливостей; приватні ключі/gas/підписи потребують іншої моделі ризику |

## Дані, інфраструктура та інтерфейс — 9 джерел

| Джерело | Що корисного | Без нього / важлива межа |
|---|---|---|
| [Apache Arrow](https://github.com/apache/arrow) | Обмін великими колонковими наборами даних | Більше серіалізації/копій або інший формат |
| [DuckDB](https://github.com/duckdb/duckdb) | Швидкі локальні запити до історії | Повільніші/складніші аналітичні запити; не фінансовий журнал |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | Дослідницькі джерела ширшої фінансової інформації | Більше окремих data-конекторів; права кожного джерела окремі |
| [Hummingbot Dashboard](https://github.com/hummingbot/dashboard) | Приклади огляду ботів та результатів | Власні екрани; готова панель не гарантує NVDA |
| [ASP.NET Core](https://github.com/dotnet/aspnetcore) | Host/API платформи .NET | Інший серверний стек; не потрібно копіювати весь репозиторій |
| [WPF](https://github.com/dotnet/wpf) | Windows-оболонка та нативна аварійна поверхня | Інша desktop-технологія; не потрібно копіювати весь framework |
| [React](https://github.com/facebook/react) | Побудова повторно використовуваних Web-компонентів | Можливий звичайний JS/інший UI; сам React не створює доступності |
| [OpenTelemetry .NET](https://github.com/open-telemetry/opentelemetry-dotnet) | Діагностика шляхів запитів і затримок | Більше власної observability; не логувати секрети |
| [Playwright](https://github.com/microsoft/playwright) | Автотести браузерних сценаріїв і клавіатури | Менше автоматичних UI-регресій; реальний NVDA-тест не замінює |

## Додаткове нове джерело, поза початковими 60

[Accessible Trading Terminal](https://github.com/churst90/accessible-trade-terminal): прямий конкурент і джерело перевірюваних UX-ідей для незрячих. Корисні напрямки: клавіатура, озвучений перегляд заявок, sonification, канали повідомлень. GPL-3.0; не скопійований, не запущений нами, не позначений інтегрованим. Нове дослідницьке посилання не збільшує число робочих адаптерів.

Поіменні статуси/умови залишаються у [реєстрі](../../control/external_reuse/catalog.json), а новий кандидат — [окремим записом](../../control/external_reuse/accessible_terminal_candidate.json).
