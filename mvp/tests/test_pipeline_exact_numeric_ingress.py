from decimal import Decimal, localcontext
import json

import pytest

from autotrade_mvp.pipeline import (
    EconomicLedger,
    handle_market_data,
    handle_portfolio,
    run_vertical_slice,
)


class HostileDecimal(Decimal):
    pass


def _checkpoint(*, initial_cash="10000", postings=None, fills=None):
    return {
        "schema_version": 1,
        "symbol": "SIM",
        "initial_cash": initial_cash,
        "postings": [] if postings is None else postings,
        "fills": {} if fills is None else fills,
        "evidence_ids": [],
        "evidence_records": {},
    }


def test_market_data_preserves_float_seam_but_rejects_decimal_subclasses():
    assert handle_market_data([100.125]) == [Decimal("100.12500000")]

    with pytest.raises(ValueError, match="Prices must be finite and positive"):
        handle_market_data([HostileDecimal("100.125")])


def test_checkpoint_initial_cash_resource_bomb_fails_before_new_durable_mutation(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(
        json.dumps(_checkpoint(initial_cash="1e999999999999999999999999")),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Corrupt checkpoint financial scalar"):
        run_vertical_slice(["100", "101", "102"], tmp_path)

    assert not (tmp_path / "journal.sqlite3").exists()
    assert not (tmp_path / "learning-evidence.jsonl").exists()
    assert not (tmp_path / "order-intents").exists()


def test_checkpoint_posting_resource_bomb_fails_closed_during_reconciliation(tmp_path):
    fills = {
        "intent-1": {
            "fill_id": "fill-1",
            "client_order_id": "intent-1",
            "symbol": "SIM",
            "side": "BUY",
            "quantity": "1",
            "price": "100",
            "fee": "0.1",
        }
    }
    postings = [
        {
            "fill_id": "fill-1",
            "cash_delta": "-100.1",
            "position_delta": "1e999999999999999999999999",
            "fee": "0.1",
        }
    ]
    (tmp_path / "checkpoint.json").write_text(
        json.dumps(_checkpoint(postings=postings, fills=fills)),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        run_vertical_slice(["100", "101", "102"], tmp_path)

    assert not (tmp_path / "journal.sqlite3").exists()
    assert not (tmp_path / "learning-evidence.jsonl").exists()


def test_ledger_and_portfolio_are_independent_of_ambient_decimal_context():
    ledger = EconomicLedger(
        Decimal("1000"),
        [
            {
                "fill_id": "fill-context",
                "cash_delta": "0",
                "position_delta": "2",
                "fee": "0",
            }
        ],
    )

    with localcontext() as context:
        context.prec = 2
        context.Emax = 9
        context.Emin = -9
        observed = handle_portfolio(ledger, Decimal("123.456789"))

    assert observed == Decimal("1246.91357800")
