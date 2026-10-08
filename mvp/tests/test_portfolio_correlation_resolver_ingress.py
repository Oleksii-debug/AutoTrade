from collections.abc import Mapping

import pytest

from mvp.autotrade_mvp.portfolio_correlation import _evidence_by_pair


class HostileResolver(Mapping):
    def __init__(self, calls):
        self.calls = calls

    def __getitem__(self, key):
        self.calls.append(("getitem", key))
        raise AssertionError("hostile resolver lookup executed")

    def __iter__(self):
        self.calls.append(("iter", None))
        raise AssertionError("hostile resolver iteration executed")

    def __len__(self):
        self.calls.append(("len", None))
        raise AssertionError("hostile resolver length executed")


class HostileKey(str):
    def __new__(cls, value, calls):
        instance = super().__new__(cls, value)
        instance.calls = calls
        return instance

    def __hash__(self):
        self.calls.append(("hash", str(self)))
        return str.__hash__(self)

    def __eq__(self, other):
        self.calls.append(("eq", other))
        raise AssertionError("hostile resolver key equality executed")


def test_mapping_subclass_fails_before_resolver_callbacks():
    calls = []
    resolver = HostileResolver(calls)

    with pytest.raises(TypeError, match="exact dict"):
        _evidence_by_pair(
            (),
            resolver,
            active=frozenset(),
            environment="SIMULATION",
            decision_time="2026-10-04T12:00:00Z",
        )

    assert calls == []


def test_dict_with_polymorphic_key_fails_before_key_callbacks():
    calls = []
    resolver = {HostileKey("evidence-1", calls): object()}
    # Ignore callbacks needed to construct the caller-owned dictionary. The
    # assessment boundary must not execute any more of them.
    calls.clear()

    with pytest.raises(TypeError, match="keys must be exact str"):
        _evidence_by_pair(
            (),
            resolver,
            active=frozenset(),
            environment="SIMULATION",
            decision_time="2026-10-04T12:00:00Z",
        )

    assert calls == []
