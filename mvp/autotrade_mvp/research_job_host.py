"""Authenticated host-service seam for canonical durable research jobs.

This adapter intentionally does not create another scheduler, job database or
authorization model. It composes ResearchJobStore with SecurityBoundary so host
surfaces can submit/read/cancel research work without exposing generic financial
job execution.
"""

from __future__ import annotations

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
    ) -> None:
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
        self._authorize(session=session, actor=actor, roles=_SUBMIT_ROLES)
        return self._jobs.enqueue(
            kind=kind,
            dedupe_key=dedupe_key,
            input_hashes=input_hashes,
            resource_budget=resource_budget,
            lease_requeueable=lease_requeueable,
            job_id=job_id,
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
