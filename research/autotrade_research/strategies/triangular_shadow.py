"""Executable top-book triangular-arbitrage shadow arithmetic."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json


SCHEMA = "autotrade-whitebit-triangular-shadow-v1"


def _decimal(value, name: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite decimal") from None
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{name} must be a finite decimal")
    return result


@dataclass(frozen=True)
class BookTop:
    market: str
    bid_price: str
    bid_quantity: str
    ask_price: str
    ask_quantity: str
    received_at: datetime
    digest: str

    def __post_init__(self) -> None:
        if type(self.market) is not str or "_" not in self.market:
            raise ValueError("market must be a canonical pair")
        for name in ("bid_price", "bid_quantity", "ask_price", "ask_quantity"):
            _decimal(getattr(self, name), name, positive=True)
        if Decimal(self.ask_price) <= Decimal(self.bid_price):
            raise ValueError("book must have a positive spread")
        if type(self.received_at) is not datetime or self.received_at.tzinfo is not timezone.utc:
            raise ValueError("received_at must use UTC timezone")
        if (type(self.digest) is not str or not self.digest.startswith("sha256:")
                or len(self.digest) != 71):
            raise ValueError("book digest must be canonical sha256")


def _pair(source: str, target: str, books: dict[str, BookTop]):
    direct = f"{source}_{target}"
    reverse = f"{target}_{source}"
    if direct in books:
        return direct, "SELL"
    if reverse in books:
        return reverse, "BUY"
    raise ValueError(f"missing conversion market for {source}->{target}")


def evaluate_cycle(
    path: tuple[str, str, str, str],
    *,
    books: dict[str, BookTop],
    taker_fees: dict[str, str],
    notional: str = "100",
    impact_reserve_bps: str = "2",
    now: datetime,
    maximum_age_seconds: int = 3,
    maximum_skew_seconds: int = 2,
) -> dict:
    if (type(path) is not tuple or len(path) != 4 or path[0] != path[-1]
            or len(set(path[:-1])) != 3):
        raise ValueError("cycle must contain three unique assets and return to origin")
    if type(books) is not dict or type(taker_fees) is not dict:
        raise TypeError("books and taker fees must be exact dictionaries")
    if type(now) is not datetime or now.tzinfo is not timezone.utc:
        raise ValueError("now must use UTC timezone")
    if type(maximum_age_seconds) is not int or not 1 <= maximum_age_seconds <= 30:
        raise ValueError("maximum age is invalid")
    if type(maximum_skew_seconds) is not int or not 0 <= maximum_skew_seconds <= 30:
        raise ValueError("maximum skew is invalid")
    amount = _decimal(notional, "notional", positive=True)
    reserve = _decimal(impact_reserve_bps, "impact reserve")
    if reserve < 0:
        raise ValueError("impact reserve cannot be negative")
    used: list[BookTop] = []
    legs = []
    start = amount
    for source, target in zip(path, path[1:]):
        market, side = _pair(source, target, books)
        book = books[market]
        if type(book) is not BookTop:
            raise TypeError("book values must be exact BookTop instances")
        if book.market != market:
            raise ValueError("book key and canonical market differ")
        fee = _decimal(taker_fees.get(market), "taker fee")
        if not 0 <= fee < 1:
            raise ValueError("taker fee is invalid")
        price = Decimal(book.bid_price if side == "SELL" else book.ask_price)
        available = Decimal(book.bid_quantity if side == "SELL" else book.ask_quantity)
        capacity = available if side == "SELL" else available * price
        if amount > capacity:
            result = {
                "schema": SCHEMA, "path": list(path), "status": "INSUFFICIENT_TOP_DEPTH",
                "market": market, "required_input": format(amount, "f"),
                "available_input": format(capacity, "f"), "network_orders": False,
                "trading_authority": False, "qualified_for_shadow_observation": False,
            }
            result["result_sha256"] = sha256(json.dumps(result, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            return result
        output_before_fee = amount * price if side == "SELL" else amount / price
        output = output_before_fee * (Decimal(1) - fee)
        legs.append({
            "market": market, "side": side, "source_asset": source,
            "target_asset": target, "input": format(amount, "f"),
            "price": format(price, "f"), "taker_fee_rate": format(fee, "f"),
            "output_after_fee": format(output, "f"), "book_sha256": book.digest,
        })
        used.append(book)
        amount = output
    oldest = min(item.received_at for item in used)
    newest = max(item.received_at for item in used)
    age = (now - oldest).total_seconds()
    skew = (newest - oldest).total_seconds()
    if age < 0:
        raise ValueError("book timestamp is in the future")
    net_bps = (amount / start - Decimal(1)) * Decimal(10000)
    after_reserve = net_bps - reserve
    fresh = age <= maximum_age_seconds and skew <= maximum_skew_seconds
    status = "QUALIFIED_SHADOW_OBSERVATION" if fresh and after_reserve > 0 else (
        "STALE_OR_SKEWED_BOOKS" if not fresh else "NO_AFTER_COST_EDGE"
    )
    result = {
        "schema": SCHEMA, "path": list(path), "status": status,
        "notional": format(start, "f"), "end_after_taker_fees": format(amount, "f"),
        "net_after_taker_fees_bps": format(net_bps, "f"),
        "impact_reserve_bps": format(reserve, "f"),
        "net_after_reserve_bps": format(after_reserve, "f"),
        "maximum_book_age_seconds": age, "book_skew_seconds": skew,
        "qualified_for_shadow_observation": status == "QUALIFIED_SHADOW_OBSERVATION",
        "legs": legs, "network_orders": False, "trading_authority": False,
        "limitations": ["TOP_OF_BOOK_ONLY", "PUBLIC_BOOK_EXCLUDES_RPI",
                        "NO_ATOMIC_EXECUTION_PROOF", "SIMULATION_ONLY"],
    }
    result["opportunity_key_sha256"] = sha256(json.dumps({
        "path": list(path),
        "books": [(leg["market"], leg["book_sha256"]) for leg in legs],
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result["result_sha256"] = sha256(json.dumps(result, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return result

