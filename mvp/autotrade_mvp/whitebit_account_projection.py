"""Secret-free WhiteBIT account projection for the local owner UI."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from threading import RLock
import time
from typing import Callable

from .exact_decimal import canonical_decimal_text, parse_bounded_exact_decimal
from .whitebit_credentials import resolve_whitebit_read_credentials
from research.autotrade_research.integrations.whitebit_private_readonly import (
    PrivateReadObservation,
    WhitebitPrivateReader,
)


_ASSET = re.compile(r"^[A-Z0-9]{1,24}$")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate WhiteBIT account field")
        value[key] = item
    return value


def _decode(observation: PrivateReadObservation) -> object:
    if type(observation) is not PrivateReadObservation:
        raise TypeError("WhiteBIT account observation differs")
    return json.loads(
        observation.body,
        object_pairs_hook=_strict_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite value")),
    )


def _asset(value: object) -> str:
    if type(value) is not str or _ASSET.fullmatch(value) is None:
        raise ValueError("WhiteBIT account asset differs")
    return value


def _amount(value: object) -> str:
    if type(value) not in (str, int) or isinstance(value, bool):
        raise ValueError("WhiteBIT account amount differs")
    parsed = parse_bounded_exact_decimal(value)
    if parsed < 0:
        raise ValueError("WhiteBIT account amount is negative")
    return canonical_decimal_text(parsed)


def project_whitebit_account(
    main: PrivateReadObservation,
    trading: PrivateReadObservation,
) -> dict[str, object]:
    """Convert the two authenticated READ responses into a bounded UI value."""

    main_value = _decode(main)
    trading_value = _decode(trading)
    if type(main_value) is not list or type(trading_value) is not dict:
        raise ValueError("WhiteBIT account response differs")

    wallet: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in main_value:
        if type(entry) is not dict:
            raise ValueError("WhiteBIT main balance entry differs")
        ticker = _asset(entry.get("ticker"))
        if ticker in seen:
            raise ValueError("duplicate WhiteBIT main balance asset")
        seen.add(ticker)
        amount = _amount(entry.get("balance"))
        if amount != "0":
            wallet.append({"asset": ticker, "balance": amount})

    spot: list[dict[str, str]] = []
    for raw_asset, raw_balance in trading_value.items():
        ticker = _asset(raw_asset)
        if type(raw_balance) is not dict:
            raise ValueError("WhiteBIT trading balance entry differs")
        available = _amount(raw_balance.get("available"))
        frozen = _amount(raw_balance.get("freeze"))
        if available != "0" or frozen != "0":
            spot.append({"asset": ticker, "available": available, "frozen": frozen})

    wallet.sort(key=lambda item: item["asset"])
    spot.sort(key=lambda item: item["asset"])
    observed = max(main.observed_at, trading.observed_at).astimezone(timezone.utc)
    return {
        "schema": "autotrade-whitebit-account-projection-v1",
        "provider": "WHITEBIT",
        "environment": "LIVE",
        "account_id": "whitebit-primary",
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "verified_account_binding": (
            main.verified_account_binding is True
            and trading.verified_account_binding is True
        ),
        "read_only": True,
        "wallet_balances": wallet,
        "spot_balances": spot,
        # Canonical owner-UI projection. These are authenticated trading-account
        # balances only; wallet funds are kept separate above.
        "balances": deepcopy(spot),
        "source_sha256": [main.raw_sha256, trading.raw_sha256],
    }


class WhitebitAccountProjectionProvider:
    """Small TTL cache so repeated UI refreshes do not spend private quota."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 5.0,
        credential_resolver: Callable[..., object] = resolve_whitebit_read_credentials,
        reader_factory: Callable[..., object] = WhitebitPrivateReader,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(ttl_seconds) not in (int, float) or not 1 <= ttl_seconds <= 60:
            raise ValueError("WhiteBIT account cache TTL differs")
        if not all(callable(item) for item in (credential_resolver, reader_factory, monotonic)):
            raise TypeError("WhiteBIT account projection callbacks must be callable")
        self._ttl = float(ttl_seconds)
        self._credential_resolver = credential_resolver
        self._reader_factory = reader_factory
        self._monotonic = monotonic
        self._cached: dict[str, object] | None = None
        self._cached_until = 0.0
        self._lock = RLock()

    def __call__(self) -> dict[str, object]:
        with self._lock:
            point = self._monotonic()
            if self._cached is not None and point < self._cached_until:
                return deepcopy(self._cached)
            credentials = self._credential_resolver(profile="whitebit-primary")
            with self._reader_factory(
                credentials.api_key,
                credentials.api_secret,
                enable_network=True,
            ) as reader:
                value = project_whitebit_account(
                    reader.main_balance(), reader.trading_balance()
                )
            if value["verified_account_binding"] is not True:
                raise ValueError("WhiteBIT account binding is not verified")
            self._cached = deepcopy(value)
            self._cached_until = point + self._ttl
            return deepcopy(value)


__all__ = ["WhitebitAccountProjectionProvider", "project_whitebit_account"]
