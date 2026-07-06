"""Enforceable identity and RBAC: verified claims, stored authority, tenant isolation.

Replaces simulation-only identity seams. Identity is established only from a
signature-verified OIDC assertion turned into an expiring, revocable session;
authority is derived from stored tenant/role/grant records, never from caller text;
and every store query is tenant-scoped. See ``docs/ADAPTER_MATURITY.md``.
"""

from .accounts import (
    ADMIN_ROLE,
    LocalAccountService,
    LocalUserStore,
    LoginResult,
    UserAccount,
    hash_password,
    verify_password,
)
from .errors import (
    AuthenticationError,
    AuthorizationError,
    CrossTenantError,
    GrantExpiredError,
    IdentityConfigurationError,
    IdentityError,
    SelfApprovalError,
    SessionExpiredError,
    SessionRevokedError,
    StepUpRequiredError,
)
from .models import (
    IDENTITY_SCHEMA_VERSION,
    AuthorityGrant,
    Claims,
    Group,
    Identity,
    Membership,
    Organization,
    PrincipalKind,
    Role,
    Session,
    Tenant,
)
from .policy import AuthorityResolver
from .service import IdentityApprovalAuthority
from .sessions import SessionManager, default_assurance
from .store import IDENTITY_MIGRATIONS, IdentityStore, grant_is_active
from .tokens import (
    Hs256Signer,
    Hs256Verifier,
    OidcValidator,
    ProviderConfig,
    SignatureVerifier,
    assert_production_identity,
)

__all__ = [
    "ADMIN_ROLE",
    "IDENTITY_MIGRATIONS",
    "IDENTITY_SCHEMA_VERSION",
    "AuthenticationError",
    "LocalAccountService",
    "LocalUserStore",
    "LoginResult",
    "UserAccount",
    "hash_password",
    "verify_password",
    "AuthorityGrant",
    "AuthorityResolver",
    "AuthorizationError",
    "Claims",
    "CrossTenantError",
    "GrantExpiredError",
    "Group",
    "Hs256Signer",
    "IdentityConfigurationError",
    "Hs256Verifier",
    "Identity",
    "IdentityApprovalAuthority",
    "IdentityError",
    "IdentityStore",
    "Membership",
    "OidcValidator",
    "Organization",
    "PrincipalKind",
    "ProviderConfig",
    "Role",
    "SelfApprovalError",
    "Session",
    "SessionExpiredError",
    "SessionManager",
    "SessionRevokedError",
    "SignatureVerifier",
    "StepUpRequiredError",
    "Tenant",
    "assert_production_identity",
    "default_assurance",
    "grant_is_active",
]
