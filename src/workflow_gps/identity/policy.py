"""Authority resolution and policy decisions.

Authority is derived from *stored* records — authority grants, group roles, and
the role permissions they reference — for the caller's own tenant only. A token's
claimed roles are never consulted, so a caller cannot self-assign authority. Each
decision raises a specific :class:`AuthorizationError` subclass naming the policy
that blocked it (cross-tenant, self-approval, expired grant, step-up required).
"""

from __future__ import annotations

from datetime import UTC, datetime

from .errors import (
    AuthorizationError,
    CrossTenantError,
    SelfApprovalError,
    SessionExpiredError,
    SessionRevokedError,
    StepUpRequiredError,
)
from .models import Session
from .store import IdentityStore, grant_is_active


def _permission_matches(held: str, required: str) -> bool:
    if held == "*" or held == required:
        return True
    held_action, _, held_scope = held.partition(":")
    req_action, _, req_scope = required.partition(":")
    return held_action == req_action and held_scope == "*"


class AuthorityResolver:
    def __init__(self, store: IdentityStore):
        self._store = store

    # ------------------------------------------------------------------ #
    def guard_tenant(self, session: Session, tenant_id: str) -> None:
        """Refuse any access to a tenant other than the session's own."""
        if session.tenant_id != tenant_id:
            raise CrossTenantError(
                f"principal in tenant {session.tenant_id!r} may not access "
                f"tenant {tenant_id!r}"
            )

    def effective_permissions(
        self, session: Session, *, now: datetime | None = None
    ) -> set[str]:
        """Permissions a principal holds in its tenant, from stored grants/roles."""
        moment = now or datetime.now(UTC)
        permissions: set[str] = set()
        role_names: set[str] = set()

        for grant in self._store.list_grants(session.tenant_id, session.principal_id):
            if grant_is_active(grant, now=moment):
                role_names.add(grant.role_name)

        membership = self._store.get_membership(session.tenant_id, session.principal_id)
        if membership is not None:
            for group_id in membership.group_ids:
                group = self._store.get_group(session.tenant_id, group_id)
                if group is not None:
                    role_names.update(group.role_names)

        for name in role_names:
            role = self._store.get_role(session.tenant_id, name)
            if role is not None:
                permissions.update(role.permissions)
        return permissions

    def has_permission(
        self, session: Session, permission: str, *, now: datetime | None = None
    ) -> bool:
        held = self.effective_permissions(session, now=now)
        return any(_permission_matches(item, permission) for item in held)

    # ------------------------------------------------------------------ #
    def _ensure_active(self, session: Session, moment: datetime) -> None:
        if session.revoked:
            raise SessionRevokedError("session has been revoked")
        if moment > session.expires_at:
            raise SessionExpiredError("session has expired")

    def authorize(
        self,
        session: Session,
        *,
        action: str,
        scope: str,
        requester_id: str | None = None,
        required_assurance: int = 1,
        now: datetime | None = None,
    ) -> bool:
        """Core gate for reviewer/approver authority.

        Order matters: session liveness, then self-action, then assurance, then the
        stored permission. Raises a specific error; returns ``True`` on success.
        """
        moment = now or datetime.now(UTC)
        self._ensure_active(session, moment)
        if requester_id is not None and session.principal_id == requester_id:
            raise SelfApprovalError(
                f"{session.principal_id!r} may not {action} their own request"
            )
        if session.assurance_level < required_assurance:
            raise StepUpRequiredError(
                f"{action}:{scope} requires assurance {required_assurance}, "
                f"session has {session.assurance_level}"
            )
        if not self.has_permission(session, f"{action}:{scope}", now=moment):
            raise AuthorizationError(
                f"{session.principal_id!r} lacks authority to {action}:{scope}"
            )
        return True

    def can_approve(
        self,
        session: Session,
        *,
        policy: str,
        requester_id: str,
        required_assurance: int = 1,
        now: datetime | None = None,
    ) -> bool:
        return self.authorize(
            session,
            action="approve",
            scope=policy,
            requester_id=requester_id,
            required_assurance=required_assurance,
            now=now,
        )

    def can_review(
        self,
        session: Session,
        *,
        policy: str,
        requester_id: str,
        required_assurance: int = 1,
        now: datetime | None = None,
    ) -> bool:
        return self.authorize(
            session,
            action="review",
            scope=policy,
            requester_id=requester_id,
            required_assurance=required_assurance,
            now=now,
        )
