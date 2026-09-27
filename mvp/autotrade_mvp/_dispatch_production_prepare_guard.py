"""Require durable canonical order preparation before PAPER/LIVE provider send."""

from __future__ import annotations

from functools import wraps
from typing import Any

from .dispatch import GuardedDispatcher


class _MissingDurableOrderPreparation(PermissionError):
    """Internal fail-closed signal consumed by GuardedDispatcher preparation handling."""


def install_production_prepare_guard() -> None:
    """Make the existing pre-send preparation boundary mandatory in production modes.

    GuardedDispatcher already owns the durable SubmissionPrepared/Blocked/Sending state
    machine and the canonical pre-send callback seam.  This installation layer does not
    introduce another dispatcher or order authority; it only prevents PAPER/LIVE callers
    from bypassing that existing seam by omitting the callback.
    """

    original = GuardedDispatcher.dispatch
    if getattr(original, "_autotrade_requires_production_prepare", False):
        return

    @wraps(original)
    def guarded_dispatch(self: GuardedDispatcher, *args: Any, **kwargs: Any):
        if self.environment in {"PAPER", "LIVE"} and kwargs.get("prepare_order") is None:
            def missing_prepare_order(*_args: Any, **_kwargs: Any) -> None:
                raise _MissingDurableOrderPreparation(
                    "durable canonical order preparation is required before provider send"
                )

            kwargs["prepare_order"] = missing_prepare_order
        return original(self, *args, **kwargs)

    guarded_dispatch._autotrade_requires_production_prepare = True  # type: ignore[attr-defined]
    GuardedDispatcher.dispatch = guarded_dispatch
