"""Journal-backed host command API with authenticated CONFIRM_INTENT dispatch.

The retained implementation remains the authority for every host journal and
operation semantic.  This shim only carries the already-present command actor
through the existing submit call so CONFIRM_INTENT canonicalization can bind it
after the retained session/origin/action authentication succeeds.
"""

from __future__ import annotations

from typing import Mapping

from .durable_host_api_legacy import *  # noqa: F401,F403
from .durable_host_api_legacy import JournalBackedHostCommandStore as _LegacyStore
from . import operator_authority_commands as _operator_commands
from .operator_authority_commands import authenticated_host_actor_scope

# The old module exposed several private helpers that existing white-box tests
# import directly.  The dispatch shim must not silently narrow that compatibility
# surface while every non-CONFIRM action still delegates to the exact retained
# implementation.
for _name in dir(_operator_commands._legacy):
    if _name not in vars(_operator_commands):
        setattr(
            _operator_commands,
            _name,
            getattr(_operator_commands._legacy, _name),
        )


class JournalBackedHostCommandStore(_LegacyStore):
    """Existing durable host store plus scoped authenticated-actor propagation."""

    def submit(self, command: Mapping[str, object]):
        actor = command.get("actor") if isinstance(command, Mapping) else None
        with authenticated_host_actor_scope(actor):
            return super().submit(command)
