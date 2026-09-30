"""Compatibility alias to the production-packaged strict JSON authority."""

import sys
from autotrade_runtime import strict_json as _implementation

sys.modules[__name__] = _implementation
