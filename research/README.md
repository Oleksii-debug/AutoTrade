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
