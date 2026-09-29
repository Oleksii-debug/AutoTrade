"""Safe simulated/paper-trading vertical slice for AutoTrade."""

from .pipeline import RunResult, run_multi_episode, run_vertical_slice, verify_replay
from ._dispatch_production_prepare_guard import install_production_prepare_guard

install_production_prepare_guard()

__all__ = ["RunResult", "run_multi_episode", "run_vertical_slice", "verify_replay"]
