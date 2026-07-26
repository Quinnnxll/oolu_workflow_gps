"""Seller KYC (M1): a verified legal identity before anything lists.

The marketplace's seller gate made real: a seller applies as a legal
entity, the deterministic screen sorts (or refuses) the application before
any human looks, and a reviewer with approve authority decides. Verified
status is what `CatalogService` reads through the gateway's
``seller:{tenant}:{principal}`` convention — no verified record, no
published listing (an MVP boundary, not a preference).

Everything rides the standing KYC machinery from ``nodeplace/kyc.py`` —
the same ``KycStore``, the same records, the same mailbox screen — minus
the Supernode account coupling: a seller answers for their own listings,
not for a node fleet. One person, one standing application: re-applying
after a rejection is allowed; re-applying over a verified or pending
record is refused.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..nodeplace.kyc import (
    KycRecord,
    KycStatus,
    KycStore,
    screen_company_email,
    trusted_domains_from_env,
)
from .errors import MarketplaceError


class SellerKycError(MarketplaceError):
    """The application cannot be taken as submitted."""


def seller_kyc_key(tenant: str, principal: str) -> str:
    return f"seller:{tenant}:{principal}"


class SellerKyc:
    """Apply, look up, queue, decide — the seller side of the trust layer."""

    def __init__(
        self,
        conn,
        *,
        audit,
        trusted_domains: frozenset[str] | None = None,
    ) -> None:
        self._store = KycStore(conn)
        self._audit = audit
        self._trusted = (
            trusted_domains
            if trusted_domains is not None
            else trusted_domains_from_env()
        )

    def status_for(self, *, tenant: str, principal: str) -> KycRecord | None:
        record = self._store.get(seller_kyc_key(tenant, principal))
        if record is None or record.tenant != tenant:
            return None
        return record

    def is_verified(self, *, tenant: str, principal: str) -> bool:
        record = self.status_for(tenant=tenant, principal=principal)
        return record is not None and record.status is KycStatus.VERIFIED

    def pending(self, *, tenant: str) -> list[KycRecord]:
        """The reviewer's queue, fast-tracked screens first."""
        return [
            record
            for record in self._store.records(status=KycStatus.PENDING_REVIEW)
            if record.tenant == tenant
            and record.node_id.startswith("seller:")
        ]

    def apply(
        self,
        *,
        tenant: str,
        principal: str,
        legal_name: str,
        company_email: str,
        registration_no: str = "",
    ) -> KycRecord:
        """File the application. The screen refuses personal mailboxes
        outright and sorts the rest; nobody is verified here — a human
        with approve authority decides every application."""
        if not legal_name.strip():
            raise SellerKycError("the legal name is required")
        current = self.status_for(tenant=tenant, principal=principal)
        if current is not None and current.status is KycStatus.VERIFIED:
            raise SellerKycError("this seller is already verified")
        if current is not None and current.status is KycStatus.PENDING_REVIEW:
            raise SellerKycError("an application is already under review")
        try:
            screen, note = screen_company_email(
                company_email, trusted_domains=self._trusted
            )
        except ValueError as exc:
            raise SellerKycError(str(exc)) from exc
        record = KycRecord(
            node_id=seller_kyc_key(tenant, principal),
            tenant=tenant,
            applicant=principal,
            legal_name=legal_name.strip(),
            company_email=company_email.strip().lower(),
            registration_no=registration_no.strip(),
            screen=screen,
            screen_note=note,
        )
        self._store.upsert(record)
        self._audit.append(
            "market.seller_kyc.applied",
            {
                "tenant": tenant,
                "principal": principal,
                "legal_name": record.legal_name,
                "screen": screen.value,
            },
        )
        return record

    def decide(
        self,
        *,
        tenant: str,
        principal: str,
        reviewer: str,
        approved: bool,
        note: str = "",
    ) -> KycRecord:
        """Record a reviewer's verdict. Authority is the route's job —
        this method only refuses decisions on nothing or on the decided."""
        current = self.status_for(tenant=tenant, principal=principal)
        if current is None:
            raise SellerKycError("no seller application here")
        if current.status is not KycStatus.PENDING_REVIEW:
            raise SellerKycError(
                f"the application is already {current.status.value}"
            )
        record = current.model_copy(
            update={
                "status": KycStatus.VERIFIED if approved else KycStatus.REJECTED,
                "reviewer": reviewer,
                "decision_note": note,
                "decided_at": datetime.now(UTC),
            }
        )
        self._store.upsert(record)
        self._audit.append(
            "market.seller_kyc.decided",
            {
                "tenant": tenant,
                "principal": principal,
                "reviewer": reviewer,
                "approved": approved,
            },
        )
        return record
