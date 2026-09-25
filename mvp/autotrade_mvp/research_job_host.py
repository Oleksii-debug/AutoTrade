"""Authenticated host-service seam for canonical durable research jobs.

This adapter intentionally does not create another scheduler, job database or
authorization model. It composes ResearchJobStore with SecurityBoundary so host
surfaces can submit/read/cancel research work without exposing generic financial
job execution.
"""

from __future__ import annotations

from hashlib import sha256
import secrets
from typing import Callable

from research.autotrade_research.jobs import ResearchJobStore

from .security import SecurityBoundary


_READ_ROLES = {"OWNER", "OPERATOR", "RESEARCHER", "OBSERVER"}
_SUBMIT_ROLES = {"OWNER", "RESEARCHER"}
_CANCEL_ROLES = {"OWNER"}


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _subject_dedupe_key(subject: str, dedupe_key: str) -> str:
    """Create an injective opaque host namespace without changing job authority."""

    normalized_subject = _text(subject, name="actor")
    normalized_key = _text(dedupe_key, name="dedupe_key")
    material = (
        len(normalized_subject).to_bytes(4, "big")
        + normalized_subject.encode("utf-8")
        + normalized_key.encode("utf-8")
    )
    return "host-subject:sha256:" + sha256(material).hexdigest()


class ResearchJobHostService:
    """Role-scoped host adapter over the one durable research-job authority."""

    def __init__(
        self,
        *,
        jobs: ResearchJobStore,
        security: SecurityBoundary,
        request_origin_provider: Callable[[], str],
    ) -> None:
        if not isinstance(jobs, ResearchJobStore):
            raise TypeError("jobs must be ResearchJobStore")
        if not isinstance(security, SecurityBoundary):
            raise TypeError("security must be SecurityBoundary")
        if not callable(request_origin_provider):
            raise TypeError("request_origin_provider must be callable")
        self._jobs = jobs
        self._security = security
        self._request_origin_provider = request_origin_provider

    def _authorize(
        self,
        *,
        session: str,
        actor: str,
        roles: set[str],
    ) -> str:
        normalized_actor = _text(actor, name="actor")
        origin = _text(
            self._request_origin_provider(),
            name="request origin",
        )
        authenticated = self._security.validate_session(
            _text(session, name="session"),
            required_roles=roles,
            origin=origin,
        )
        if not secrets.compare_digest(authenticated.subject, normalized_actor):
            raise PermissionError("Authenticated session subject does not match actor")
        return normalized_actor

    def enqueue(
        self,
        *,
        session: str,
        actor: str,
        kind: str,
        dedupe_key: str,
        input_hashes: list[str],
        resource_budget: dict[str, int | float],
        lease_requeueable: bool = False,
        job_id: str | None = None,
    ) -> tuple[dict[str, object], bool]:
        normalized_actor = self._authorize(
            session=session,
            actor=actor,
            roles=_SUBMIT_ROLES,
        )
        if job_id is not None:
            raise ValueError(
                "host research job_id is server-assigned; use dedupe_key for idempotency"
            )
        return self._jobs.enqueue(
            kind=kind,
            dedupe_key=_subject_dedupe_key(normalized_actor, dedupe_key),
            input_hashes=input_hashes,
            resource_budget=resource_budget,
            lease_requeueable=lease_requeueable,
            job_id=None,
        )

    def get(
        self,
        job_id: str,
        *,
        session: str,
        actor: str,
    ) -> dict[str, object]:
        self._authorize(session=session, actor=actor, roles=_READ_ROLES)
        return self._jobs.get(job_id)

    def cancel(
        self,
        job_id: str,
        *,
        session: str,
        actor: str,
    ) -> bool:
        # The current durable job record has no submitter ownership field.
        # Until ownership is represented durably, cancellation is Owner-only;
        # a Researcher session cannot cancel another researcher's job by ID.
        self._authorize(session=session, actor=actor, roles=_CANCEL_ROLES)
        return self._jobs.cancel(job_id)
