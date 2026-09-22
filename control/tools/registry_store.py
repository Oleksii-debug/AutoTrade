"""Atomic Git storage primitive for a trusted registry service.

This is storage, not authentication, readiness admission, or worker authorization.
Concurrent workers remain disabled until those deployment gates are qualified.
Each writer uses its own bare cache. No checkout, index or user branch is changed.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from control.tools.registry_state import _validate_registry, RegistryProtocolError


class RegistryWriteUnconfirmed(RuntimeError):
    """Re-read and reconcile the request ID; never assume a failed push had no effect."""


class GitRegistryStore:
    REF = "refs/heads/control/registry"

    def __init__(self, cache: Path, remote: str):
        self.cache = Path(cache)
        self.remote = remote
        if not remote or remote.startswith("-"):
            raise ValueError("explicit repository remote required")

    def _git(self, *args: str, data: str | None = None) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.cache), *args], input=data,
            text=True, encoding="utf-8", capture_output=True, timeout=60,
        )
        if completed.returncode:
            # Avoid leaking credentials embedded in remote URLs through git stderr.
            raise RegistryWriteUnconfirmed("Git registry operation failed; refresh and reconcile")
        return completed.stdout.strip()

    def read(self) -> tuple[str, dict, str]:
        self._git("fetch", "--no-tags", self.remote, self.REF)
        head = self._git("rev-parse", "FETCH_HEAD")
        state = json.loads(self._git("show", f"{head}:registry.json"))
        _validate_registry(state)
        log = self._git("show", f"{head}:transitions.ndjson")
        return head, state, log + "\n" if log else ""

    def prepare(self, expected_head: str, next_state: dict, transition: dict) -> str:
        if not re.fullmatch(r"[0-9a-f]{40}", expected_head):
            raise ValueError("expected_head must be a complete commit SHA")
        _validate_registry(next_state)
        old_state = json.loads(self._git("show", f"{expected_head}:registry.json"))
        _validate_registry(old_state)
        if next_state["generation"] != old_state["generation"] + 1:
            raise RegistryProtocolError("persist exactly one generation transition")
        if not isinstance(transition.get("request_id"), str) or not transition["request_id"]:
            raise RegistryProtocolError("transition request_id required")
        old_log = self._git("show", f"{expected_head}:transitions.ndjson")
        event = {**transition, "parent": expected_head, "generation": next_state["generation"]}
        content = {
            "registry.json": json.dumps(next_state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            "transitions.ndjson": (old_log + "\n" if old_log else "") + json.dumps(event, sort_keys=True) + "\n",
        }
        entries = {}
        for line in self._git("ls-tree", expected_head).splitlines():
            meta, name = line.split("\t", 1)
            entries[name] = meta
        for name, value in content.items():
            blob = self._git("hash-object", "-w", "--stdin", data=value)
            entries[name] = f"100644 blob {blob}"
        tree = self._git("mktree", data="".join(f"{entries[n]}\t{n}\n" for n in sorted(entries)))
        # Exactly one parent is required for non-force push to act as optimistic CAS.
        return self._git(
            "-c", "user.name=AutoTrade registry", "-c", "user.email=registry@autotrade.invalid",
            "commit-tree", tree, "-p", expected_head, data="Record registry transition\n",
        )

    def publish(self, prepared_head: str, expected_head: str) -> None:
        if not all(re.fullmatch(r"[0-9a-f]{40}", v) for v in (prepared_head, expected_head)):
            raise ValueError("complete commit SHAs required")
        if self._git("show", "-s", "--format=%P", prepared_head) != expected_head:
            raise RegistryProtocolError("prepared commit must have exactly the expected parent")
        self._git("push", "--porcelain", self.remote, f"{prepared_head}:{self.REF}")
        observed = self._git("ls-remote", self.remote, self.REF).split()
        if not observed or observed[0] != prepared_head:
            raise RegistryWriteUnconfirmed("Registry advanced; refresh and reconcile request ID")
