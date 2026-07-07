"""Supernode KYC: a verified legal entity earns global trust.

A Supernode — a group, corporation, or government division with humans in
full control — can obey the KYC policy. Verification rides on a paying
subscription (the fee funds the reviewing work), and a verified Supernode
carries a GLOBAL trust-ranking multiplier that flows down to every node
under it: marketplace ranking favors work whose responsible legal entity
is known. That is the whole point twice over — fraud has nowhere to rank,
and legal entities win workflow.

Reviewing is filtered BEFORE any human looks, deterministically:

- a personal/free mailbox (gmail.com, outlook.com, ...) is refused
  outright — it cannot anchor a legal entity;
- a company mailbox on the operator's trusted-domain list (for example a
  Google-verified workspace domain) is fast-tracked;
- anything else queues for standard review.

The screen never verifies anyone by itself: a human reviewer with
approve authority decides every application. The filter only sorts the
queue and throws out what could never pass.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .errors import ContributionError, OwnershipError

# The paying plans whose fee carries the reviewing work.
PAYING_PLANS = frozenset({"plus", "pro", "enterprise"})

# The global trust-ranking multiplier a verified Supernode carries; it
# flows to every node under it (nested Supernodes included).
VERIFIED_TRUST_MULTIPLIER = 1.5

# Free/personal mailbox providers: refused outright — a personal mailbox
# cannot anchor a legal entity, whoever holds it.
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "mail.com",
        "gmx.com",
        "gmx.net",
        "qq.com",
        "163.com",
        "126.com",
        "yandex.com",
        "yandex.ru",
    }
)


class KycStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    VERIFIED = "verified"
    REJECTED = "rejected"


class KycScreen(str, Enum):
    FAST_TRACK = "fast_track"  # trusted company domain — reviewed first
    STANDARD = "standard"  # unknown domain — the ordinary queue


class KycRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    node_id: str  # the Supernode
    tenant: str
    applicant: str
    legal_name: str
    company_email: str
    registration_no: str = ""
    screen: KycScreen
    screen_note: str = ""
    status: KycStatus = KycStatus.PENDING_REVIEW
    decision_note: str = ""
    reviewer: str | None = None
    # Stamped when verified; 1.0 otherwise. What ranking multiplies by.
    multiplier: float = 1.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None


def screen_company_email(
    email: str, *, trusted_domains: frozenset[str]
) -> tuple[KycScreen, str]:
    """The filter in front of the reviewers. Raises ValueError on what
    could never pass; returns which queue everything else lands in."""
    address = (email or "").strip().lower()
    local, _, domain = address.partition("@")
    if not local or "." not in domain:
        raise ValueError(f"'{email}' is not a mailbox address")
    if domain in FREE_MAIL_DOMAINS:
        raise ValueError(
            f"a company mailbox is required — {domain} is a personal "
            "mailbox provider and cannot anchor a legal entity"
        )
    if domain in trusted_domains:
        return KycScreen.FAST_TRACK, f"{domain} is a trusted company domain"
    return KycScreen.STANDARD, f"{domain} is unrecognized — standard review"


def trusted_domains_from_env() -> frozenset[str]:
    """OOLU_KYC_TRUSTED_DOMAINS: comma-separated domains the operator has
    verified out of band (e.g. Google-verified workspace domains)."""
    raw = os.environ.get("OOLU_KYC_TRUSTED_DOMAINS", "")
    return frozenset(
        d.strip().lower() for d in raw.split(",") if d.strip()
    )


_SCHEMA = """CREATE TABLE IF NOT EXISTS kyc_records (
    node_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
)"""


class KycStore:
    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def upsert(self, record: KycRecord) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO kyc_records (node_id, payload_json)
                   VALUES (?, ?)
                   ON CONFLICT(node_id) DO UPDATE SET
                     payload_json = excluded.payload_json""",
                (record.node_id, record.model_dump_json()),
            )

    def get(self, node_id: str) -> KycRecord | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM kyc_records WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        if row is None:
            return None
        return KycRecord.model_validate_json(row["payload_json"])


class KycService:
    """Apply / decide / look up — and the multiplier ranking reads."""

    def __init__(
        self,
        store: KycStore,
        *,
        accounts,  # nodeplace.NodeAccountStore
        plan_for=None,  # (tenant) -> subscription plan name
        trusted_domains: frozenset[str] | None = None,
        multiplier: float = VERIFIED_TRUST_MULTIPLIER,
    ) -> None:
        self._store = store
        self._accounts = accounts
        self._plan_for = plan_for or (lambda tenant: "free")
        self._trusted = (
            trusted_domains
            if trusted_domains is not None
            else trusted_domains_from_env()
        )
        self._multiplier = multiplier

    # ------------------------------------------------------------------ #
    def status_for(self, node_id: str) -> KycRecord | None:
        return self._store.get(node_id)

    def apply(
        self,
        node_id: str,
        *,
        tenant: str,
        principal: str,
        legal_name: str,
        company_email: str,
        registration_no: str = "",
    ) -> KycRecord:
        account = self._accounts.get(node_id)
        if account is None:
            raise ContributionError(f"node '{node_id}' has no account")
        if not account.is_supernode:
            raise ValueError(
                "KYC verification is for Supernodes — the legal entity "
                "answering for many nodes"
            )
        if principal not in {account.responsible, account.admin}:
            raise OwnershipError(
                "only the Supernode's responsible or admin may apply"
            )
        current = self._store.get(node_id)
        if current is not None and current.status is KycStatus.VERIFIED:
            raise ValueError("this Supernode is already verified")
        if current is not None and current.status is KycStatus.PENDING_REVIEW:
            raise ValueError("an application is already under review")
        if not (legal_name or "").strip():
            raise ValueError("the legal entity's name is required")
        # Screen BEFORE the plan gate: an application that could never
        # pass is refused without asking anyone to subscribe first.
        screen, note = screen_company_email(
            company_email, trusted_domains=self._trusted
        )
        plan = str(self._plan_for(tenant) or "free")
        if plan not in PAYING_PLANS:
            raise SubscriptionRequired(
                "KYC verification rides on a paying plan — its fee funds "
                "the reviewing work. Subscribe in the account console first."
            )
        record = KycRecord(
            node_id=node_id,
            tenant=tenant,
            applicant=principal,
            legal_name=legal_name.strip(),
            company_email=company_email.strip().lower(),
            registration_no=(registration_no or "").strip(),
            screen=screen,
            screen_note=note,
        )
        self._store.upsert(record)
        return record

    def decide(
        self, node_id: str, *, reviewer: str, approved: bool, note: str = ""
    ) -> KycRecord:
        """A human's verdict. Authority is the route's job (approve
        authority in the tenant) — this method records the decision."""
        current = self._store.get(node_id)
        if current is None:
            raise ContributionError(f"no KYC application for '{node_id}'")
        if current.status is not KycStatus.PENDING_REVIEW:
            raise ValueError(f"the application is already {current.status.value}")
        record = current.model_copy(
            update={
                "status": KycStatus.VERIFIED if approved else KycStatus.REJECTED,
                "multiplier": self._multiplier if approved else 1.0,
                "reviewer": reviewer,
                "decision_note": note,
                "decided_at": datetime.now(UTC),
            }
        )
        self._store.upsert(record)
        return record

    # ------------------------------------------------------------------ #
    def trust_multiplier(self, node_id: str) -> float:
        """The global ranking multiplier for ANY node: its own verified
        Supernode standing, or the nearest verified Supernode above it
        (membership chains walk upward; the best ancestor counts once —
        multipliers never stack)."""
        best = 1.0
        seen: set[str] = set()
        current_id: str | None = node_id
        while current_id and current_id not in seen:
            seen.add(current_id)
            account = self._accounts.get(current_id)
            if account is None:
                break
            if account.is_supernode:
                record = self._store.get(current_id)
                if record is not None and record.status is KycStatus.VERIFIED:
                    best = max(best, record.multiplier)
            current_id = account.supernode_id
        return best


class SubscriptionRequired(ValueError):
    """KYC asked for without the paying plan that carries its fee."""
