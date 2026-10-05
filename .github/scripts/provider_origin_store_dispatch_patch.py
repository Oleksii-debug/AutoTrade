from pathlib import Path


path = Path("mvp/autotrade_mvp/provider_origin.py")
source = path.read_text(encoding="utf-8")

anchor = '''from .provider_response_limits import (\n    HARD_MAX_PROVIDER_RESPONSE_BYTES,\n    require_provider_response_bytes,\n)\n'''
retained = '''from .provider_response_limits import (\n    HARD_MAX_PROVIDER_RESPONSE_BYTES,\n    require_provider_response_bytes,\n)\n\n\n# Retain the installed storage executables once, rather than redispatching\n# through mutable public classes after this authority module is composed.\n_PROVIDER_ORIGIN_APPEND_EVENT = JournalStore.append_event\n_PROVIDER_ORIGIN_LOAD_EVENTS = JournalStore.load_events\n_PROVIDER_ORIGIN_PUBLISH_BYTES = ArtifactStore.publish_bytes\n_PROVIDER_ORIGIN_READ_AUTHENTICATED_SNAPSHOT = (\n    ArtifactStore.read_authenticated_snapshot\n)\n'''
assert source.count(anchor) == 1
assert "_PROVIDER_ORIGIN_APPEND_EVENT" not in source
source = source.replace(anchor, retained, 1)

replacements = {
    "JournalStore.append_event(": "_PROVIDER_ORIGIN_APPEND_EVENT(",
    "JournalStore.load_events(": "_PROVIDER_ORIGIN_LOAD_EVENTS(",
    "ArtifactStore.publish_bytes(": "_PROVIDER_ORIGIN_PUBLISH_BYTES(",
    "ArtifactStore.read_authenticated_snapshot(": (
        "_PROVIDER_ORIGIN_READ_AUTHENTICATED_SNAPSHOT("
    ),
}
expected_counts = {
    "JournalStore.append_event(": 6,
    "JournalStore.load_events(": 5,
    "ArtifactStore.publish_bytes(": 1,
    "ArtifactStore.read_authenticated_snapshot(": 3,
}
for old, new in replacements.items():
    actual = source.count(old)
    assert actual == expected_counts[old], (old, actual)
    source = source.replace(old, new)

for old in replacements:
    assert old not in source

path.write_text(source, encoding="utf-8")
