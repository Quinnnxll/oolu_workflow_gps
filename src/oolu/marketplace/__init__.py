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
    ProtocolViolation,
    SellerUnverified,
    StrongAuthenticationRequired,
    WrongState,
)
from .federation import FederationDesk, ImportedOffer, PeerMarket, SourcedOffer
from .fraud import OrderHistory, RiskSignals
from .inventory import InventoryService, Reservation
from .jobdispatch import WorkerLeaseDispatcher, node_capability
from .jobs import ExecutionJob, JobDesk
from .milestones import MilestoneBook, MilestoneState
from .models import (
    ApprovalRecord,
    CommercialIntent,
    ExecutionAuthorization,
    IntentAction,
    Offer,
)
from .negotiation import (
    NegotiationBounds,
    NegotiationLedger,
    NegotiationSession,
    OutsideBounds,
    proposal_violations,
)
from .orders import (
    DEFAULT_TAKE_RATE_BPS,
    OrderRecord,
    OrderService,
    OrderStore,
    StoredOrder,
)
from .orgcontrol import (
    DelayNotElapsed,
    PayoutChangeDesk,
    PayoutChangeRequest,
    SelfApproval,
)
from .partners import (
    FinancingQuote,
    InsuranceQuote,
    SimpleInterestFinancing,
    financing_offer,
    insurance_offer,
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
from .protocol import PROTOCOL_VERSION, sign_offer, verify_offer
from .reconciliation import ReconciliationDesk, ReconciliationReport
from .recurring import RecurringBook, RecurringObligation, RequiresNewApproval
from .review import approval_summary, required_approvers, required_strength
from .rfq import (
    QuoteRecord,
    QuoteRefused,
    RequestForQuote,
    RfqService,
    RfqSpecification,
    specification_gaps,
)
from .sellerkyc import SellerKyc, SellerKycError, seller_kyc_key
from .service import MarketplaceSpine
from .store import (
    ApprovalStore,
    IntentStore,
    PolicyStore,
    SalesPolicyStore,
    StoredIntent,
)

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
    "InventoryService",
    "NegotiationBounds",
    "NegotiationLedger",
    "NegotiationSession",
    "OrderHistory",
    "OutsideBounds",
    "QuoteRecord",
    "QuoteRefused",
    "RequestForQuote",
    "Reservation",
    "RfqService",
    "RfqSpecification",
    "RiskSignals",
    "SalesPolicyStore",
    "proposal_violations",
    "specification_gaps",
    "DelayNotElapsed",
    "ExecutionJob",
    "JobDesk",
    "MilestoneBook",
    "MilestoneState",
    "PayoutChangeDesk",
    "PayoutChangeRequest",
    "ReconciliationDesk",
    "ReconciliationReport",
    "RecurringBook",
    "RecurringObligation",
    "RequiresNewApproval",
    "SelfApproval",
    "WorkerLeaseDispatcher",
    "FederationDesk",
    "FinancingQuote",
    "ImportedOffer",
    "InsuranceQuote",
    "PROTOCOL_VERSION",
    "PeerMarket",
    "ProtocolViolation",
    "SimpleInterestFinancing",
    "SourcedOffer",
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
    "SellerKyc",
    "SellerKycError",
    "SellerUnverified",
    "StoredIntent",
    "StoredOrder",
    "StrongAuthenticationRequired",
    "WrongState",
    "approval_summary",
    "delegation_gaps",
    "financing_offer",
    "insurance_offer",
    "node_capability",
    "sign_offer",
    "verify_offer",
    "seller_kyc_key",
    "evaluate_purchase",
    "evaluate_sale",
    "intent_digest",
    "required_approvers",
    "required_strength",
]
