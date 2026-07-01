"""Identity and authorization error hierarchy.

Authentication failures (who you are) are kept distinct from authorization
failures (what you may do), and the authorization subclasses name the specific
policy violation so call sites and tests can assert on the precise reason.
"""

from __future__ import annotations


class IdentityError(RuntimeError):
    """Base class for identity-layer failures."""


class IdentityConfigurationError(IdentityError):
    pass


class AuthenticationError(IdentityError):
    """The caller's identity could not be established from a verifiable claim."""


class SessionExpiredError(AuthenticationError):
    """The session is past its expiry."""


class SessionRevokedError(AuthenticationError):
    """The session was explicitly revoked."""


class AuthorizationError(IdentityError):
    """The established principal is not permitted to perform the action."""


class CrossTenantError(AuthorizationError):
    """An attempt to read or act on a tenant other than the caller's own."""


class SelfApprovalError(AuthorizationError):
    """A principal attempted to approve or review their own request."""


class GrantExpiredError(AuthorizationError):
    """The authority grant required for the action has expired or was revoked."""


class StepUpRequiredError(AuthorizationError):
    """The action requires a higher authentication assurance than the session holds."""
