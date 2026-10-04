import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from control.tools.registry_store import (
    GitRegistryStore,
    RegistryProtocolError,
    RegistryWriteUnconfirmed,
)
from test_registry_state import NOW, claim, empty_registry, request


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, text=True, capture_output=True).stdout.strip()


class RegistryStorageTests(unittest.TestCase):
    def test_competing_parent_commits_have_one_winner_and_retry_preserves_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, seed = root / "remote.git", root / "seed"
            git(root, "init", "--bare", str(remote))
            git(root, "init", str(seed))
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            (seed / "registry.json").write_text(json.dumps(empty_registry()), encoding="utf-8")
            (seed / "transitions.ndjson").write_text("", encoding="utf-8")
            (seed / "REGISTRY.md").write_text("Preserve me", encoding="utf-8")
            git(seed, "add", ".")
            git(seed, "commit", "-m", "Seed")
            git(seed, "push", str(remote), "HEAD:refs/heads/control/registry")
            stores = []
            for name in ("a.git", "b.git"):
                git(root, "init", "--bare", str(root / name))
                stores.append(GitRegistryStore(root / name, str(remote)))
            a, b = stores
            h1, old1, _ = a.read()
            h2, old2, _ = b.read()
            next1, _ = claim(old1, request(), expected_generation=0, now=NOW)
            next2, _ = claim(old2, request("second", "second", ["src/free"]), expected_generation=0, now=NOW)
            prepared1 = a.prepare(h1, next1, {"request_id": "req-a", "operation": "CLAIM"})
            prepared2 = b.prepare(h2, next2, {"request_id": "second", "operation": "CLAIM"})
            a.publish(prepared1, h1)
            with self.assertRaises(RegistryWriteUnconfirmed):
                b.publish(prepared2, h2)
            new_head, current, log = b.read()
            self.assertEqual(len(current["claims"]), 1)
            retry, _ = claim(current, request("second", "second", ["src/free"]), expected_generation=1, now=NOW)
            retried = b.prepare(new_head, retry, {"request_id": "second", "operation": "CLAIM"})
            b.publish(retried, new_head)
            final_head, final, final_log = a.read()
            self.assertEqual(final["generation"], 2)
            self.assertTrue(final_log.startswith(log))
            self.assertEqual(len(final_log.splitlines()), 2)
            self.assertEqual(git(a.cache, "show", f"{final_head}:REGISTRY.md"), "Preserve me")


    def test_publish_rejects_non_descendant_before_remote_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, seed = root / "remote.git", root / "seed"
            git(root, "init", "--bare", str(remote))
            git(root, "init", str(seed))
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            (seed / "registry.json").write_text(
                json.dumps(empty_registry()),
                encoding="utf-8",
            )
            (seed / "transitions.ndjson").write_text("", encoding="utf-8")
            git(seed, "add", ".")
            git(seed, "commit", "-m", "Seed")
            git(seed, "push", str(remote), "HEAD:refs/heads/control/registry")

            cache = root / "writer.git"
            git(root, "init", "--bare", str(cache))
            store = GitRegistryStore(cache, str(remote))
            expected_head, old, _ = store.read()
            next_state, _ = claim(
                old,
                request(),
                expected_generation=0,
                now=NOW,
            )
            prepared = store.prepare(
                expected_head,
                next_state,
                {"request_id": "req-a", "operation": "CLAIM"},
            )
            tree = git(cache, "show", "-s", "--format=%T", prepared)
            non_descendant = git(
                cache,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit-tree",
                tree,
                "-p",
                prepared,
                "-m",
                "wrong parent for expected-tip publish",
            )

            with self.assertRaises(RegistryProtocolError):
                store.publish(non_descendant, expected_head)

            self.assertEqual(
                git(cache, "ls-remote", str(remote), GitRegistryStore.REF).split()[0],
                expected_head,
            )

    def test_deleted_remote_ref_cannot_be_recreated_by_stale_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote, seed = root / "remote.git", root / "seed"
            git(root, "init", "--bare", str(remote))
            git(root, "init", str(seed))
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            (seed / "registry.json").write_text(
                json.dumps(empty_registry()),
                encoding="utf-8",
            )
            (seed / "transitions.ndjson").write_text("", encoding="utf-8")
            git(seed, "add", ".")
            git(seed, "commit", "-m", "Seed")
            git(seed, "push", str(remote), "HEAD:refs/heads/control/registry")

            cache = root / "writer.git"
            git(root, "init", "--bare", str(cache))
            store = GitRegistryStore(cache, str(remote))
            expected_head, old, _ = store.read()
            next_state, _ = claim(
                old,
                request(),
                expected_generation=0,
                now=NOW,
            )
            prepared = store.prepare(
                expected_head,
                next_state,
                {"request_id": "req-a", "operation": "CLAIM"},
            )

            # Simulate authority-ref loss after read/prepare but before publish.
            # A plain fast-forward push would recreate the missing branch.
            git(remote, "update-ref", "-d", GitRegistryStore.REF)
            self.assertEqual(
                git(cache, "ls-remote", str(remote), GitRegistryStore.REF),
                "",
            )

            with self.assertRaises(RegistryWriteUnconfirmed):
                store.publish(prepared, expected_head)

            self.assertEqual(
                git(cache, "ls-remote", str(remote), GitRegistryStore.REF),
                "",
            )
