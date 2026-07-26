"""The embedded marketplace's commercial spine (docs/marketplace-build-plan.md, M0).

Typed commercial intents, an immutable intent digest, the six-decision
deterministic policy ladder, revocable agent delegation, and digest-bound
approvals. No catalog, no orders, no money — the law ships before the
market does.
"""

from .delegation import AgentDelegation, DelegationStore, delegation_gaps
from .digest import DIGEST_FIELDS, intent_digest
from .errors import (
    ApprovalConsumed,
    ApprovalExpired,
    DelegationBlocked,
    DigestMismatch,
    DuplicateApprover,
    IntentExpired,
    MarketNotFound,
    MarketplaceError,
    StrongAuthenticationRequired,
    WrongState,
)
from .models import (
    ApprovalRecord,
    CommercialIntent,
    ExecutionAuthorization,
    IntentAction,
    Offer,
)
from .policy import (
    Decision,
    PolicyVerdict,
    PurchaseFacts,
    PurchasePolicy,
    SaleFacts,
    SalesPolicy,
    evaluate_purchase,
    evaluate_sale,
)
from .review import approval_summary, required_approvers, required_strength
from .service import MarketplaceSpine
from .store import ApprovalStore, IntentStore, PolicyStore, StoredIntent

__all__ = [
    "AgentDelegation",
    "ApprovalConsumed",
    "ApprovalExpired",
    "ApprovalRecord",
    "ApprovalStore",
    "CommercialIntent",
    "Decision",
    "DelegationBlocked",
    "DelegationStore",
    "DigestMismatch",
    "DIGEST_FIELDS",
    "DuplicateApprover",
    "ExecutionAuthorization",
    "IntentAction",
    "IntentExpired",
    "IntentStore",
    "MarketNotFound",
    "MarketplaceError",
    "MarketplaceSpine",
    "Offer",
    "PolicyStore",
    "PolicyVerdict",
    "PurchaseFacts",
    "PurchasePolicy",
    "SaleFacts",
    "SalesPolicy",
    "StoredIntent",
    "StrongAuthenticationRequired",
    "WrongState",
    "approval_summary",
    "delegation_gaps",
    "evaluate_purchase",
    "evaluate_sale",
    "intent_digest",
    "required_approvers",
    "required_strength",
]
