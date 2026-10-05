"""Temporary guarded patch for ZERO durable-scope alias conflict hardening.

This file exists only while PR #1725 is converging.  The temporary workflow
runs this patch against the latest branch head, executes focused regressions,
and removes both this script and itself in the verified publication commit.
"""
from pathlib import Path


PATH = Path("mvp/autotrade_mvp/simulation_runtime_checkpoint.py")


def _replace_between(text: str, start: str, end: str, replacement: str, label: str) -> str:
    if text.count(start) != 1:
        raise SystemExit(f"{label}: expected one start marker, found {text.count(start)}")
    start_index = text.index(start)
    tail = text[start_index:]
    if tail.count(end) != 1:
        raise SystemExit(f"{label}: expected one end marker after start, found {tail.count(end)}")
    end_index = start_index + tail.index(end)
    return text[:start_index] + replacement.rstrip() + "\n\n\n" + text[end_index:]


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    if "def _payload_scope_values(" in text:
        print("ZERO scope alias hardening is already present; source patch is a no-op")
        return

    helpers = '''def _payload_scope_values(
    payload: Mapping[str, object],
    key: str,
) -> tuple[object, ...]:
    """Collect every explicit non-null scope marker across durable layouts.

    Ownership is a security boundary: a locally matching marker must never hide
    a contradictory marker in another supported envelope layout.
    """

    values: list[object] = []

    def append(mapping: object) -> None:
        if type(mapping) is dict and key in mapping:
            value = mapping.get(key)
            if value is not None:
                values.append(value)

    append(payload)
    append(payload.get("scope"))
    identity = payload.get("identity")
    if type(identity) is dict:
        append(identity.get("scope"))
    append(payload.get("request"))
    return tuple(values)


def _payload_scope_value(
    payload: Mapping[str, object],
    key: str,
) -> object | None:
    """Resolve the first marker for legacy callers; authority checks inspect all."""

    values = _payload_scope_values(payload, key)
    return values[0] if values else None


def _payload_provider_ids(payload: Mapping[str, object]) -> tuple[object, ...]:
    """Collect canonical provider ids plus the durable submission alias."""

    return (
        *_payload_scope_values(payload, "provider_id"),
        *_payload_scope_values(payload, "provider"),
    )


def _payload_provider_id(payload: Mapping[str, object]) -> object | None:
    """Resolve one provider marker for compatibility; authority checks inspect all."""

    providers = _payload_provider_ids(payload)
    return providers[0] if providers else None


def _component_scope_values(
    event: Mapping[str, object],
    key: str,
) -> tuple[object, ...]:
    payload = event.get("payload")
    payload = payload if type(payload) is dict else {}
    values: list[object] = []
    if key in event and event.get(key) is not None:
        values.append(event.get(key))
    values.extend(_payload_scope_values(payload, key))
    return tuple(values)


def _component_environment(event: Mapping[str, object]) -> object | None:
    values = _component_scope_values(event, "environment")
    return values[0] if values else None


def _component_host_id(event: Mapping[str, object]) -> object | None:
    values = _component_scope_values(event, "host_id")
    return values[0] if values else None'''

    foreign = '''def _component_event_definitively_foreign(
    event: Mapping[str, object],
    *,
    financial_scope: Mapping[str, str] | None = None,
) -> bool:
    """Exclude any component fact carrying an explicit foreign scope marker.

    Durable schemas expose scope through several compatible layouts. Every
    explicit marker is authoritative for conflict detection so a matching alias
    cannot mask a contradictory account, provider, environment, or host value.
    Missing markers remain checkpoint authority; positive publication ownership
    is enforced separately.
    """

    payload = event.get("payload")
    payload = payload if type(payload) is dict else {}
    environments = _component_scope_values(event, "environment")
    if any(value != "SIMULATION" for value in environments):
        return True

    supported_environments = payload.get("environments")
    if (
        type(supported_environments) in {list, tuple}
        and "SIMULATION" not in supported_environments
    ):
        return True

    hosts = _component_scope_values(event, "host_id")
    if any(value not in _LOCAL_COMPONENT_HOST_IDS for value in hosts):
        return True

    if financial_scope is not None:
        accounts = _payload_scope_values(payload, "account_id")
        if any(value != financial_scope["account_id"] for value in accounts):
            return True
        providers = _payload_provider_ids(payload)
        if any(value != financial_scope["provider_id"] for value in providers):
            return True
    return False'''

    positive = '''def _component_event_has_positive_run_scope(
    event: Mapping[str, object],
    *,
    financial_scope: Mapping[str, str],
) -> bool:
    """Require positive run scope with no contradictory durable alias."""

    if _component_event_definitively_foreign(
        event,
        financial_scope=financial_scope,
    ):
        return False
    payload = event.get("payload")
    if type(payload) is not dict:
        return False

    environments = _component_scope_values(event, "environment")
    if environments:
        if any(value != financial_scope["environment"] for value in environments):
            return False
    else:
        supported_environments = payload.get("environments")
        if (
            type(supported_environments) not in {list, tuple}
            or financial_scope["environment"] not in supported_environments
        ):
            return False

    accounts = _payload_scope_values(payload, "account_id")
    if not accounts or any(
        value != financial_scope["account_id"] for value in accounts
    ):
        return False
    providers = _payload_provider_ids(payload)
    if any(value != financial_scope["provider_id"] for value in providers):
        return False
    return True'''

    text = _replace_between(
        text,
        "def _payload_scope_value(\n",
        "def _autonomous_run_financial_scope(\n",
        helpers,
        "scope helper block",
    )
    text = _replace_between(
        text,
        "def _component_event_definitively_foreign(\n",
        "def _component_event_has_positive_run_scope(\n",
        foreign,
        "foreign-scope predicate",
    )
    text = _replace_between(
        text,
        "def _component_event_has_positive_run_scope(\n",
        "def _component_aggregate_events(\n",
        positive,
        "positive-scope predicate",
    )
    PATH.write_text(text, encoding="utf-8")
    print("Applied ZERO durable-scope alias conflict hardening")


if __name__ == "__main__":
    main()
