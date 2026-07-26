"""The commercial spine's refusals — each one a named law, not a message.

Every error here is a deterministic gate refusing to move: wrong state,
expired promise, changed terms, revoked authority. None of them is advisory —
a caller cannot acknowledge its way past any of these the way a budget review
can be acknowledged. The gateway maps them to HTTP; the service raises them.
"""

from __future__ import annotations


class MarketplaceError(Exception):
    """Base for every commercial-spine refusal."""


class MarketNotFound(MarketplaceError):
    """No such record in the caller's tenant. Cross-tenant reads land here
    too — another tenant's intent is indistinguishable from none at all."""


class WrongState(MarketplaceError):
    """The intent is not in a state where this transition is legal."""


class IntentExpired(MarketplaceError):
    """The intent's own promise window closed before the transition."""


class ApprovalExpired(MarketplaceError):
    """The approval outlived its expiration; a fresh review is required."""


class ApprovalConsumed(MarketplaceError):
    """The approval was already spent. Approvals are single-use."""


class DigestMismatch(MarketplaceError):
    """The live terms no longer match the digest the human approved.

    This is the spec's central acceptance test: approval of digest A can
    never execute digest B. A price bump, a version bump, a counterparty
    swap — any material change lands here, and the answer is always a new
    intent and a new approval cycle, never a tolerance."""


class StrongAuthenticationRequired(MarketplaceError):
    """The verdict demands step-up authentication this session lacks."""


class DuplicateApprover(MarketplaceError):
    """One person cannot count twice toward a multi-approver requirement."""


class DelegationBlocked(MarketplaceError):
    """The agent's delegation is revoked, expired, or absent — every
    unexecuted intent it minted is blocked immediately."""
