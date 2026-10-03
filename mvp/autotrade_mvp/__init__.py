"""Safe simulated/paper-trading vertical slice for AutoTrade."""

from .pipeline import RunResult, run_multi_episode, run_vertical_slice, verify_replay

# Install the fail-closed PAPER/LIVE provider-fill capability seal after the
# established vertical-slice imports are available. Existing function objects
# retained by pipeline code still resolve the patched implementation globals.
from . import _provider_fill_atomic_authority as _provider_fill_atomic_authority  # noqa: F401,E402

__all__ = ["RunResult", "run_multi_episode", "run_vertical_slice", "verify_replay"]
