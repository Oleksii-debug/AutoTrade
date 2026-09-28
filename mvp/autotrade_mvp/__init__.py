"""Safe simulated/paper-trading vertical slice for AutoTrade."""

from .pipeline import RunResult, run_multi_episode, run_vertical_slice, verify_replay

__all__ = ["RunResult", "run_multi_episode", "run_vertical_slice", "verify_replay"]
