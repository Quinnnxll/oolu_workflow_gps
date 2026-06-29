"""Bridge from verified identity to the orchestrator's approval records.

The orchestrator consumes ``ApprovalRecord``s. This authority only mints one after
:class:`AuthorityResolver` confirms the *session* (not caller text) is permitted to
approve the given policy for the given requester, at the required assurance. The
record carries an evidence hash binding it to the session, so an approval cannot be
fabricated without an authorized, verified identity.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from ..skills.models import ApprovalRecord
from .models import Session
from .policy import AuthorityResolver


class IdentityApprovalAuthority:
    def __init__(self, resolver: AuthorityResolver):
        self._resolver = resolver

    def approve(
        self,
        session: Session,
        *,
        run_id: str,
        policy: str,
        requester_id: str,
        required_assurance: int = 1,
        scope: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        # Raises a specific AuthorizationError if the session is not permitted.
        self._resolver.can_approve(
            session,
            policy=policy,
            requester_id=requester_id,
            required_assurance=required_assurance,
            now=now,
        )
        evidence = hashlib.sha256(
            f"{session.session_id}|{run_id}|{policy}".encode()
        ).hexdigest()
        return ApprovalRecord(
            principal=session.principal_id,
            policy=policy,
            decision="approved",
            scope={**(scope or {}), "tenant_id": session.tenant_id, "run_id": run_id},
            evidence_hash=evidence,
            created_at=now or datetime.now(UTC),
        )
