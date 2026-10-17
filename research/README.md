# AutoTrade research package

This Python package is isolated from the future financial-authority runtime.

Current imported primitives:
- strict JSON ingestion;
- durable whole-file artifact/config publication;
- local advisory resource locking.

These utilities MUST NOT become an alternative financial journal, distributed execution fence or trading-authority path.

Install for development:

`python -m pip install -e research`

Run tests:

`python -m unittest discover -s research/tests -v`

## Optional open-source research adapters (owner wave 2026-10-17)

The module `autotrade_research.integrations.open_source_shadow` adds original,
read-only bridges for public OHLCV via CCXT, River mature-label streaming
classification and Optuna bounded research threshold trials. **These do not
place orders, promote models or grant financial authority.**

Opt-in installation in an isolated research environment:

`python -m pip install -e research && python -m pip install -r research/optional-open-source-requirements.txt`

Isolated mock characterizations:

`PYTHONPATH=research python -m unittest discover -s research/tests -p test_open_source_shadow.py -v`

Real upstream dependency execution, provider connectivity, Windows/Linux tests
and Plan10 integration are **not yet qualified**. Read the
[60-source registry](../control/external_reuse/catalog.json).
