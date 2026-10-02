"""Compatibility facade for the neutral strict-JSON authority.

Production owns strict JSON decoding under autotrade_runtime. Research imports
must resolve to the exact same module object so security-critical parser state,
exception classes, and helper identities cannot diverge.
"""

from __future__ import annotations

from importlib import import_module
import sys

sys.modules[f"{__name__}.strict_json"] = import_module("autotrade_runtime.strict_json")
