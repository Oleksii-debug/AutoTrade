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
