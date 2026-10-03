"""Safe simulated/paper-trading vertical slice for AutoTrade."""

from .pipeline import RunResult, run_multi_episode, run_vertical_slice, verify_replay
from . import _authority_snapshot_write_seal as _authority_snapshot_write_seal

__all__ = ["RunResult", "run_multi_episode", "run_vertical_slice", "verify_replay"]