"""Versioned identity and RBAC records.

Identity (who) and authority (what) are modelled separately and explicitly:

- A :class:`Claims` is the *verified* content of an OIDC assertion — it establishes
  who the caller is, but it carries no authority.
- A :class:`Session` is a server-issued, expiring, revocable handle derived from a
  verified assertion. Authority is never read from a token's text.
- Tenants, organizations, memberships, groups, roles, and authority grants are
  *stored* records; a principal's permissions are derived from these, so a caller
  cannot self-assign a role by asserting it in a token.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

IDENTITY_SCHEMA_VERSION = 1


def _id() -> str:
    return uuid4().hex


class PrincipalKind(str, Enum):
    USER = "user"
    SERVICE = "service"
    DEVICE = "device"


# --------------------------------------------------------------------------- #
# Verified token content and the session derived from it.                     #
# --------------------------------------------------------------------------- #
class Claims(BaseModel):
    """The verified content of an OIDC assertion. Establishes identity, not authority."""

    model_config = ConfigDict(frozen=True)

    issuer: str
    subject: str
    audiences: list[str] = Field(default_factory=list)
    tenant_id: str
    principal_kind: PrincipalKind = PrincipalKind.USER
    expires_at: datetime
    not_before: datetime | None = None
    issued_at: datetime | None = None
    amr: list[str] = Field(default_factory=list)
    acr: str | None = None
    # The raw claim set, retained for audit only — never consulted for authority.
    raw: dict[str, Any] = Field(default_factory=dict)


class Session(BaseModel):
    """A server-issued, expiring, revocable identity handle."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = IDENTITY_SCHEMA_VERSION
    session_id: str = Field(default_factory=_id)
    principal_id: str
    principal_kind: PrincipalKind
    tenant_id: str
    issued_at: datetime
    expires_at: datetime
    revoked: bool = False
    assurance_level: int = 1
    amr: list[str] = Field(default_factory=list)
    source_issuer: str | None = None


# --------------------------------------------------------------------------- #
# Stored organizational and authority records.                                #
# --------------------------------------------------------------------------- #
class Tenant(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = IDENTITY_SCHEMA_VERSION
    tenant_id: str
    name: str


class Organization(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = IDENTITY_SCHEMA_VERSION
    org_id: str = Field(default_factory=_id)
    tenant_id: str
    name: str


class Identity(BaseModel):
    """A principal record (user, service, or device) homed in one tenant."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = IDENTITY_SCHEMA_VERSION
    principal_id: str
    kind: PrincipalKind
    tenant_id: str
    display_name: str | None = None
    # For service/device identities: the issuer/subject they authenticate as.
    issuer: str | None = None
    disabled: bool = False


class Role(BaseModel):
    """A named permission set, scoped to a tenant.

    Permissions are ``action:scope`` strings (e.g. ``approve:deploy``); ``*`` and
    ``action:*`` wildcards are supported by the resolver.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = IDENTITY_SCHEMA_VERSION
    tenant_id: str
    name: str
    permissions: frozenset[str] = Field(default_factory=frozenset)


class Group(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = IDENTITY_SCHEMA_VERSION
    group_id: str = Field(default_factory=_id)
    tenant_id: str
    name: str
    role_names: frozenset[str] = Field(default_factory=frozenset)


class Membership(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = IDENTITY_SCHEMA_VERSION
    tenant_id: str
    principal_id: str
    org_id: str | None = None
    group_ids: frozenset[str] = Field(default_factory=frozenset)


class AuthorityGrant(BaseModel):
    """An explicit, attributable, expiring grant of a role to a principal."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = IDENTITY_SCHEMA_VERSION
    grant_id: str = Field(default_factory=_id)
    tenant_id: str
    principal_id: str
    role_name: str
    granted_by: str
    expires_at: datetime | None = None
    revoked: bool = False
