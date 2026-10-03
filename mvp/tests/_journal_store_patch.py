"""Anti-shadow-safe JournalStore fault injection for tests.

Production authority boundaries reject instance attributes that shadow JournalStore
methods. Tests that need to inject I/O races must therefore patch the class while
scoping the replacement to exactly one selected canonical store instance.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from mvp.autotrade_mvp.persistence import JournalStore


class _ScopedJournalStoreMethodPatch:
    def __init__(self, store: JournalStore, name: str, **mock_kwargs) -> None:
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")
        if type(name) is not str or not name:
            raise TypeError("name must be exact non-empty str")
        original = getattr(JournalStore, name)
        if not callable(original):
            raise TypeError("selected JournalStore member must be callable")
        self.mock = Mock(**mock_kwargs)
        selected = store

        def scoped(instance, *args, **kwargs):
            if instance is selected:
                return self.mock(*args, **kwargs)
            return original(instance, *args, **kwargs)

        self._patcher = patch.object(JournalStore, name, new=scoped)

    def start(self) -> Mock:
        self._patcher.start()
        return self.mock

    def stop(self) -> None:
        self._patcher.stop()

    def __enter__(self) -> Mock:
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()


def patch_journal_store_method(
    store: JournalStore,
    name: str,
    **mock_kwargs,
) -> _ScopedJournalStoreMethodPatch:
    return _ScopedJournalStoreMethodPatch(store, name, **mock_kwargs)
