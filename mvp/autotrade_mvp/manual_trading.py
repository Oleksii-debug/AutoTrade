"""Manual and assisted WhiteBIT command ingress.

This module is deliberately not a provider sender.  It owns durable review
drafts and the explicit human confirmation boundary, then hands one immutable
command to the already-composed canonical OMS/sender gateway.  A missing
ORDER_MANAGEMENT capability or missing gateway fails closed before that gateway
is called.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from threading import RLock
from typing import Callable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .exact_decimal import (
    canonical_decimal_text,
    exact_multiply,
    parse_bounded_exact_decimal,
)
from .persistence import JournalStore, payload_digest


_SCHEMA = "autotrade-manual-whitebit-v1"
_AGGREGATE_TYPE = "manual_whitebit_command"
_AGGREGATE_ID = "whitebit-primary"
_DRAFT_TTL_SECONDS = 60
_CONFIRMATION = "CONFIRM REAL WHITEBIT ORDER"
_MAX_MESSAGE_CHARS = 500
_MARKET = re.compile(r"[A-Z0-9]{1,20}_[A-Z0-9]{1,20}\Z")


class ManualTradingError(ValueError):
    """A manual request is malformed or cannot cross its safety boundary."""


class ManualTradingUnavailable(PermissionError):
    """The external provider capability or canonical sender is unavailable."""


def _utc_now(clock: Callable[[], datetime]) -> tuple[datetime, str]:
    value = clock()
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError("manual trading clock must return an aware datetime")
    value = value.astimezone(timezone.utc)
    return value, value.isoformat().replace("+00:00", "Z")


def _uuid_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ManualTradingError(f"{name} must be one canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ManualTradingError(f"{name} must be one canonical UUID") from error
    if str(parsed) != value.lower():
        raise ManualTradingError(f"{name} must be one canonical UUID")
    return value.lower()


def _positive_decimal(value: object, *, name: str) -> str:
    if type(value) not in (str, int) or isinstance(value, bool):
        raise ManualTradingError(f"{name} must be an exact decimal")
    try:
        parsed = parse_bounded_exact_decimal(value)
    except (TypeError, ValueError) as error:
        raise ManualTradingError(f"{name} must be an exact decimal") from error
    if parsed <= 0:
        raise ManualTradingError(f"{name} must be positive")
    return canonical_decimal_text(parsed)


def _market(value: object) -> str:
    if type(value) is not str or _MARKET.fullmatch(value) is None:
        raise ManualTradingError("market must be canonical WhiteBIT BASE_QUOTE text")
    return value


def _decimal_places(value: str) -> int:
    return max(0, -parse_bounded_exact_decimal(value).as_tuple().exponent)


def _request_digest(value: Mapping[str, object]) -> str:
    return sha256(json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def unarmed_whitebit_capability() -> dict[str, object]:
    """Current deployment boundary: only the stored READ profile is usable."""

    return {
        "provider_id": "WHITEBIT",
        "environment": "LIVE",
        "account_id": "whitebit-primary",
        "permission_scopes": ["ACCOUNT_READ"],
        "capability_snapshot_id": None,
        "current": True,
        "credential_profile": "whitebit-primary",
        "credential_purpose": "READ",
        "forbidden_permissions": [],
        "blocker": (
            "Only stored profile whitebit-primary is READ-only. The separately observed "
            "AutoTrade_FullAccess_2026 UI key has no stored secret and has forbidden "
            "deposit, withdrawal and transfer permissions. Create a separate TRADE-purpose "
            "WhiteBIT profile limited to account reads plus Order_management with verified "
            "IP allowlist, issue exact current ORDER_WRITE capability evidence, and bind "
            "the canonical production OMS/sender."
        ),
    }


class ManualTradingController:
    """Durable review/confirm ingress for the canonical WhiteBIT order path."""

    def __init__(
        self,
        journal: JournalStore,
        *,
        quote_provider: Callable[[str], dict[str, object]],
        capability_provider: Callable[[], Mapping[str, object]] = unarmed_whitebit_capability,
        account_projection_provider: Callable[[], Mapping[str, object]] | None = None,
        canonical_command_gateway: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        submission_guard: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if type(journal) is not JournalStore:
            raise TypeError("journal must be exact JournalStore")
        if not callable(quote_provider) or not callable(capability_provider) or not callable(clock):
            raise TypeError("manual trading callbacks must be callable")
        if account_projection_provider is not None and not callable(account_projection_provider):
            raise TypeError("account_projection_provider must be callable or None")
        if canonical_command_gateway is not None and not callable(canonical_command_gateway):
            raise TypeError("canonical_command_gateway must be callable or None")
        if submission_guard is not None and not callable(submission_guard):
            raise TypeError("submission_guard must be callable or None")
        self._journal = journal
        self._quote_provider = quote_provider
        self._capability_provider = capability_provider
        self._account_projection_provider = account_projection_provider
        self._canonical_command_gateway = canonical_command_gateway
        self._submission_guard = submission_guard
        self._clock = clock
        self._lock = RLock()

    def _events(self) -> list[dict[str, object]]:
        events = self._journal.load_events_by_aggregate_type(_AGGREGATE_TYPE)
        expected = 1
        for event in events:
            if (
                event["aggregate_id"] != _AGGREGATE_ID
                or int(event["aggregate_version"]) != expected
                or type(event.get("payload")) is not dict
                or event["payload"].get("schema") != _SCHEMA
            ):
                raise ManualTradingError("manual WhiteBIT command journal is inconsistent")
            expected += 1
        return events

    def _event(self, *, kind: str, identity: str, payload: dict[str, object], now: str) -> None:
        event = {
            "event_id": str(uuid5(NAMESPACE_URL, f"autotrade:manual-whitebit:{kind}:{identity}")),
            "event_type": kind,
            "schema_version": "1.0.0",
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": _AGGREGATE_ID,
            "aggregate_version": str(self._journal.next_aggregate_version(_AGGREGATE_TYPE, _AGGREGATE_ID)),
            "host_id": "local-product",
            "owner_epoch": "1",
            "environment": "LIVE",
            "occurred_at": now,
            "observed_at": now,
            "committed_at": now,
            "correlation_id": str(uuid5(NAMESPACE_URL, f"autotrade:manual-whitebit:correlation:{identity}")),
            "causation_id": None,
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "evidence_refs": [],
        }
        self._journal.append_event(event)

    @staticmethod
    def _drafts(events: list[dict[str, object]]) -> dict[str, dict[str, object]]:
        result = {}
        for event in events:
            if event["event_type"] == "ManualWhiteBitDraftPrepared":
                draft = event["payload"].get("draft")
                if type(draft) is not dict or type(draft.get("draft_id")) is not str:
                    raise ManualTradingError("manual WhiteBIT draft is malformed")
                result[draft["draft_id"]] = draft
        return result

    @staticmethod
    def _confirmations(events: list[dict[str, object]]) -> dict[str, dict[str, object]]:
        result = {}
        for event in events:
            if event["event_type"] == "ManualWhiteBitCommandCompleted":
                draft_id = event["payload"].get("draft_id")
                if type(draft_id) is not str:
                    raise ManualTradingError("manual WhiteBIT completion is malformed")
                result[draft_id] = event["payload"]
        return result

    def market(self, market: str) -> dict[str, object]:
        requested = _market(market)
        value = self._quote_provider(requested)
        if type(value) is not dict or value.get("schema") != "autotrade-whitebit-public-quote-v1":
            raise ManualTradingError("public WhiteBIT quote contract differs")
        if value.get("public_data_only") is not True or value.get("trading_authority") is not False:
            raise ManualTradingError("public WhiteBIT quote crossed an authority boundary")
        if value.get("market") != requested or value.get("venue") != "WHITEBIT_SPOT":
            raise ManualTradingError("public WhiteBIT quote market differs")
        instrument = value.get("instrument")
        if type(instrument) is not dict or instrument.get("schema") != "autotrade-whitebit-spot-instrument-v1":
            raise ManualTradingError("current WhiteBIT SPOT instrument metadata is unavailable")
        if (
            instrument.get("market") != requested
            or instrument.get("trades_enabled") is not True
            or instrument.get("delisted_at") is not None
        ):
            raise ManualTradingError("WhiteBIT SPOT market is unknown or disabled")
        _positive_decimal(instrument.get("minimum_quantity"), name="minimum_quantity")
        _positive_decimal(instrument.get("minimum_notional"), name="minimum_notional")
        for field in ("quantity_precision", "quote_precision"):
            precision = instrument.get(field)
            if type(precision) is not int or not 0 <= precision <= 24:
                raise ManualTradingError("WhiteBIT SPOT instrument precision is invalid")
        return value

    def _capability(self) -> dict[str, object]:
        raw = self._capability_provider()
        if not isinstance(raw, Mapping):
            raise ManualTradingError("WhiteBIT capability projection is unavailable")
        value = dict(raw)
        required = {"provider_id", "environment", "account_id", "permission_scopes",
                    "capability_snapshot_id", "current", "credential_profile",
                    "credential_purpose", "forbidden_permissions", "blocker"}
        if set(value) != required:
            raise ManualTradingError("WhiteBIT capability projection differs")
        if value["provider_id"] != "WHITEBIT" or value["environment"] != "LIVE":
            raise ManualTradingError("WhiteBIT LIVE capability projection differs")
        scopes = value["permission_scopes"]
        if type(scopes) is not list or any(type(item) is not str for item in scopes):
            raise ManualTradingError("WhiteBIT permission scopes are invalid")
        forbidden = value["forbidden_permissions"]
        if type(forbidden) is not list or any(type(item) is not str for item in forbidden):
            raise ManualTradingError("WhiteBIT forbidden permissions are invalid")
        value["permission_scopes"] = list(scopes)
        value["forbidden_permissions"] = list(forbidden)
        return value

    def _order_entry_enabled(self, capability: Mapping[str, object]) -> bool:
        scopes = capability["permission_scopes"]
        return (
            capability["current"] is True
            and "ORDER_MANAGEMENT" in scopes
            and "ORDER_WRITE" in scopes
            and type(capability["capability_snapshot_id"]) is str
            and bool(capability["capability_snapshot_id"])
            and capability["credential_purpose"] == "TRADE"
            and capability["forbidden_permissions"] == []
            and self._canonical_command_gateway is not None
            and self._submission_guard is not None
        )

    def status(self, *, confirmation: Mapping[str, object] | None = None) -> dict[str, object]:
        capability = self._capability()
        account = None
        account_state = "UNAVAILABLE"
        if self._account_projection_provider is not None:
            try:
                raw = self._account_projection_provider()
                if not isinstance(raw, Mapping):
                    raise ManualTradingError("WhiteBIT account projection differs")
                account = dict(raw)
                account_state = "READY"
            except (OSError, RuntimeError, ValueError):
                # A transient account-read failure must not hide the manual
                # screen or public market data. It also never enables orders.
                account = None
                account_state = "UNAVAILABLE"
        events = self._events()
        drafts = self._drafts(events)
        completed = self._confirmations(events)
        _point, now = _utc_now(self._clock)
        enabled = self._order_entry_enabled(capability)
        return {
            "schema": _SCHEMA,
            "mode": "MANUAL_WHITEBIT",
            "provider": "WHITEBIT",
            "environment": "LIVE",
            "account_id": capability["account_id"],
            "as_of": now,
            "account_projection_status": account_state,
            "account": account,
            "permission_scopes": capability["permission_scopes"],
            "credential_profile": capability["credential_profile"],
            "credential_purpose": capability["credential_purpose"],
            "forbidden_permissions": capability["forbidden_permissions"],
            "capability_snapshot_id": capability["capability_snapshot_id"],
            "canonical_sender_bound": self._canonical_command_gateway is not None,
            "order_entry_enabled": enabled,
            "activation_blocker": None if enabled else capability["blocker"],
            "pending_drafts": sum(1 for key in drafts if key not in completed),
            "confirmation": None if confirmation is None else dict(confirmation),
            "assistant_execution_authority": False,
        }

    def prepare(self, request: Mapping[str, object], *, source: str = "MANUAL") -> dict[str, object]:
        if type(request) is not dict:
            raise ManualTradingError("manual draft request must be an object")
        if source not in {"MANUAL", "ASSISTED_CHAT"}:
            raise ManualTradingError("manual draft source is invalid")
        allowed = {
            "request_id", "market", "action", "quantity", "order_type",
            "limit_price", "client_order_id",
        }
        if set(request) - allowed:
            raise ManualTradingError("manual draft contains unsupported fields")
        request_id = _uuid_text(request.get("request_id"), name="request_id")
        market = _market(request.get("market"))
        action = request.get("action")
        if action not in {"BUY", "SELL", "HOLD", "CANCEL"}:
            raise ManualTradingError("manual action is unsupported")
        digest = _request_digest(request)
        draft_id = str(uuid5(NAMESPACE_URL, f"autotrade:manual-whitebit-draft:{source}:{request_id}:{digest}"))
        with self._lock:
            events = self._events()
            prior = self._drafts(events).get(draft_id)
            if prior is not None:
                return {"draft": prior, "requires_separate_confirmation": True}
            point, now = _utc_now(self._clock)
            quantity = order_type = limit_price = client_order_id = None
            quote = self.market(market)
            instrument = quote["instrument"]
            if action in {"BUY", "SELL"}:
                quantity = _positive_decimal(request.get("quantity"), name="quantity")
                if parse_bounded_exact_decimal(quantity) < parse_bounded_exact_decimal(
                    instrument["minimum_quantity"]
                ):
                    raise ManualTradingError("quantity is below WhiteBIT minimum_quantity")
                if _decimal_places(quantity) > instrument["quantity_precision"]:
                    raise ManualTradingError("quantity exceeds WhiteBIT quantity precision")
                order_type = request.get("order_type")
                if order_type not in {"MARKET", "LIMIT"}:
                    raise ManualTradingError("order_type must be MARKET or LIMIT")
                if order_type == "LIMIT":
                    limit_price = _positive_decimal(request.get("limit_price"), name="limit_price")
                    if _decimal_places(limit_price) > instrument["quote_precision"]:
                        raise ManualTradingError("limit price exceeds WhiteBIT quote precision")
                elif request.get("limit_price") not in (None, ""):
                    raise ManualTradingError("MARKET draft cannot contain limit_price")
                effective_price = parse_bounded_exact_decimal(
                    limit_price if limit_price is not None else
                    (quote["ask_price"] if action == "BUY" else quote["bid_price"])
                )
                notional = exact_multiply(parse_bounded_exact_decimal(quantity), effective_price)
                if notional < parse_bounded_exact_decimal(
                    instrument["minimum_notional"]
                ):
                    raise ManualTradingError("order is below WhiteBIT minimum_notional")
                client_order_id = "manual-" + draft_id.replace("-", "")[:24]
            elif action == "CANCEL":
                client_order_id = request.get("client_order_id")
                if type(client_order_id) is not str or not client_order_id.strip() or len(client_order_id) > 64:
                    raise ManualTradingError("client_order_id is required for CANCEL")
                client_order_id = client_order_id.strip()
            draft = {
                "schema": _SCHEMA,
                "draft_id": draft_id,
                "source": source,
                "action": action,
                "provider": "WHITEBIT",
                "environment": "LIVE",
                "market": market,
                "product_family": "SPOT",
                "quantity": quantity,
                "order_type": order_type,
                "limit_price": limit_price,
                "client_order_id": client_order_id,
                "prepared_at": now,
                "expires_at": (point + timedelta(seconds=_DRAFT_TTL_SECONDS)).isoformat().replace("+00:00", "Z"),
                "quote": quote,
                "instrument": instrument,
                "execution_performed": False,
            }
            payload = {"schema": _SCHEMA, "draft": draft, "request_digest": digest,
                       "financial_state_changed": False}
            self._event(kind="ManualWhiteBitDraftPrepared", identity=draft_id, payload=payload, now=now)
            return {"draft": draft, "requires_separate_confirmation": True}

    def confirm(self, request: Mapping[str, object]) -> dict[str, object]:
        if type(request) is not dict or set(request) != {"draft_id", "confirmation"}:
            raise ManualTradingError("confirmation request fields differ")
        draft_id = _uuid_text(request.get("draft_id"), name="draft_id")
        if request.get("confirmation") != _CONFIRMATION:
            raise ManualTradingError("explicit real WhiteBIT confirmation is required")
        with self._lock:
            events = self._events()
            prior = self._confirmations(events).get(draft_id)
            if prior is not None:
                return self.status(confirmation=prior)
            draft = self._drafts(events).get(draft_id)
            if draft is None:
                raise ManualTradingError("manual WhiteBIT draft does not exist")
            point, now = _utc_now(self._clock)
            expiry = datetime.fromisoformat(str(draft["expires_at"]).replace("Z", "+00:00")).astimezone(timezone.utc)
            if point > expiry:
                raise ManualTradingError("manual WhiteBIT draft expired")
            if draft["action"] == "HOLD":
                result = {"outcome": "HOLD_RECORDED", "provider_send_performed": False}
            else:
                capability = self._capability()
                if not self._order_entry_enabled(capability):
                    raise ManualTradingUnavailable(str(capability["blocker"]))
                command = {
                    "schema": "autotrade-canonical-manual-command-v1",
                    "command_id": draft_id,
                    "source": "MANUAL_CONFIRMED" if draft["source"] == "MANUAL" else "ASSISTED_CONFIRMED",
                    "capability_snapshot_id": capability["capability_snapshot_id"],
                    "draft": dict(draft),
                }
                # Final durable runtime-mode fence: immediately precedes the
                # one canonical OMS/sender handoff and cannot be bypassed by
                # manual or assisted preparation.
                self._submission_guard()
                raw = self._canonical_command_gateway(command)
                if not isinstance(raw, Mapping):
                    raise ManualTradingError("canonical OMS/sender result differs")
                result = dict(raw)
            payload = {"schema": _SCHEMA, "draft_id": draft_id, "action": draft["action"],
                       "source": draft["source"], "completed_at": now, "result": result}
            self._event(kind="ManualWhiteBitCommandCompleted", identity=draft_id, payload=payload, now=now)
            return self.status(confirmation=payload)

    def assistant_query(self, request: Mapping[str, object]) -> dict[str, object]:
        if type(request) is not dict or set(request) != {"request_id", "market", "message"}:
            raise ManualTradingError("assistant request fields differ")
        request_id = _uuid_text(request.get("request_id"), name="request_id")
        market = _market(request.get("market"))
        message = request.get("message")
        if type(message) is not str or not message.strip() or len(message) > _MAX_MESSAGE_CHARS:
            raise ManualTradingError("assistant message is invalid")
        normalized = message.strip().lower()
        quote = self.market(market)
        action = None
        if re.search(r"\b(buy|купити|купи)\b", normalized): action = "BUY"
        elif re.search(r"\b(sell|продати|продай)\b", normalized): action = "SELL"
        elif re.search(r"\b(cancel|скасувати|скасуй)\b", normalized): action = "CANCEL"
        prepared = None
        response = f"WhiteBIT {market} bid {quote['bid_price']}, ask {quote['ask_price']}. No action was executed."
        if action in {"BUY", "SELL"}:
            numbers = re.findall(r"(?<![\w.])\d+(?:[.,]\d+)?", normalized)
            if numbers:
                order_type = "LIMIT" if re.search(r"\b(limit|ліміт|лімітний|за)\b", normalized) else "MARKET"
                if order_type == "MARKET" or len(numbers) > 1:
                    prepared = self.prepare({
                        "request_id": request_id, "market": market, "action": action,
                        "quantity": numbers[0].replace(",", "."), "order_type": order_type,
                        "limit_price": numbers[1].replace(",", ".") if order_type == "LIMIT" else None,
                    }, source="ASSISTED_CHAT")
                    response = "Prepared a real WhiteBIT order draft. I did not execute it; use the separate explicit confirmation control."
        elif action == "CANCEL":
            match = re.search(r"\b(?:cancel|скасувати|скасуй)\s+([a-z0-9._:-]+)", normalized)
            if match is not None:
                prepared = self.prepare({"request_id": request_id, "market": market, "action": "CANCEL",
                                         "client_order_id": match.group(1)}, source="ASSISTED_CHAT")
                response = "Prepared a real WhiteBIT cancellation draft. I did not execute it; separate confirmation is required."
        return {
            "schema": "autotrade-assisted-whitebit-v1",
            "response": response,
            "quote": quote,
            "prepared": prepared,
            "assistant_execution_authority": False,
            "execution_performed": False,
            "requires_separate_confirmation": prepared is not None,
        }


__all__ = [
    "ManualTradingController", "ManualTradingError", "ManualTradingUnavailable",
    "unarmed_whitebit_capability",
]
