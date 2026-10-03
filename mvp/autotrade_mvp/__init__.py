"""Safe simulated/paper-trading vertical slice for AutoTrade."""

from __future__ import annotations

from typing import Any

__all__ = ["RunResult", "run_multi_episode", "run_vertical_slice", "verify_replay"]

_LAZY_PIPELINE_EXPORTS = frozenset(__all__)


def __getattr__(name: str) -> Any:
    """Load vertical-slice exports only when callers explicitly request them.

    Importing an installed host/process-composition submodule must not eagerly pull
    the complete trading pipeline into the process before its own authorities and
    packaging dependencies have been validated.
    """
    if name not in _LAZY_PIPELINE_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import pipeline

    value = getattr(pipeline, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_PIPELINE_EXPORTS)
