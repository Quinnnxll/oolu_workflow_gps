"""The embedded marketplace (docs/marketplace-build-plan.md).

M0 — the commercial spine: typed commercial intents, an immutable intent
digest, the six-decision deterministic policy ladder, revocable agent
delegation, and digest-bound approvals. The spine modules import no money
path — the law stands apart from the market.

M1 — the fixed-price market: the catalog (verified sellers, versioned
listings), the order state machine (authorize on confirmation, capture on
acceptance, refunds as compensating transactions), and the double-entry
ledger postings that make the platform's take a ledger fact.
"""

from .catalog import CatalogService, CatalogStore, Listing
from .delegation import AgentDelegation, DelegationStore, delegation_gaps
from .digest import DIGEST_FIELDS, intent_digest
from .errors import (
    ApprovalConsumed,
    ApprovalExpired,
    DelegationBlocked,
    DigestMismatch,
    DuplicateApprover,
    IntentExpired,
    ListingUnavailable,
    MarketNotFound,
    MarketplaceError,
    SellerUnverified,
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
from .orders import (
    DEFAULT_TAKE_RATE_BPS,
    OrderRecord,
    OrderService,
    OrderStore,
    StoredOrder,
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
    "CatalogService",
    "CatalogStore",
    "CommercialIntent",
    "Decision",
    "DEFAULT_TAKE_RATE_BPS",
    "DelegationBlocked",
    "DelegationStore",
    "DigestMismatch",
    "DIGEST_FIELDS",
    "DuplicateApprover",
    "ExecutionAuthorization",
    "IntentAction",
    "IntentExpired",
    "IntentStore",
    "Listing",
    "ListingUnavailable",
    "MarketNotFound",
    "MarketplaceError",
    "MarketplaceSpine",
    "Offer",
    "OrderRecord",
    "OrderService",
    "OrderStore",
    "PolicyStore",
    "PolicyVerdict",
    "PurchaseFacts",
    "PurchasePolicy",
    "SaleFacts",
    "SalesPolicy",
    "SellerUnverified",
    "StoredIntent",
    "StoredOrder",
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
