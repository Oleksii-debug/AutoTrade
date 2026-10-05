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
        # Freeze the caller-owned Mapping once before either actor propagation or
        # retained authentication reads it.  The previous wrapper read actor
        # from the original Mapping and then handed that same mutable/polymorphic
        # object to the retained submit path, allowing a Mapping implementation
        # to return actor A for the ContextVar and actor B for session validation.
        # A detached exact dict makes both steps consume one immutable identity
        # snapshot; the retained implementation still owns every validation,
        # command-dedupe, journal and non-CONFIRM semantic after this boundary.
        if not isinstance(command, Mapping):
            return super().submit(command)
        frozen_command = dict(command)
        actor = frozen_command.get("actor")
        with authenticated_host_actor_scope(actor):
            return super().submit(frozen_command)
