"""Compatibility alias to the production-packaged ResourceLock authority."""

import sys
from autotrade_runtime import resource_lock as _implementation

sys.modules[__name__] = _implementation
