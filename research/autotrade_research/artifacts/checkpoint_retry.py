"""Bounded retry for a transient Windows checkpoint replacement collision."""

import asyncio


_RETRY_DELAYS = (0.01, 0.03, 0.06)
_WINDOWS_SHARING_ERRORS = frozenset({5, 32, 33})


async def publish_checkpoint(path, payload, write) -> int:
    """Retry the same checkpoint payload while an ordinary reader releases it."""
    for retries in range(len(_RETRY_DELAYS) + 1):
        try:
            write(path, payload)
            return retries
        except PermissionError as error:
            if (getattr(error, "winerror", None) not in _WINDOWS_SHARING_ERRORS
                    or retries == len(_RETRY_DELAYS)):
                raise
        await asyncio.sleep(_RETRY_DELAYS[retries])
