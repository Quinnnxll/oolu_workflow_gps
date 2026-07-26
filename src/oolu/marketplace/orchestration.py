"""Dynamic supply orchestration (M4 closer): sourcing that survives a no.

The sweep answers "who could serve this?"; the orchestrator answers "then
get it" — deterministically. It ranks every eligible source, mints the
intent for the best one (feeding the ladder the attestation the source
actually carries, never more), and when a source fails at placement — the
shelf emptied, the peer suspended — the caller excludes it and procures
again: the next-best source gets its turn, the failed one is named on the
audit chain, and nothing is retried into the void.

No model sits anywhere in this loop. Ranking is the sweep's normalized
order; the policy ladder still judges every minted intent; approvals,
digests, and delegations apply unchanged. Orchestration changes which
offer is asked about — never what may happen to it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .catalog import CatalogService
from .errors import WrongState
from .federation import FederationDesk, SourcedOffer
from .rfq import RfqSpecification
from .service import MarketplaceSpine
from .store import StoredIntent


class SupplyOrchestrator:
    def __init__(
        self,
        *,
        federation: FederationDesk,
        spine: MarketplaceSpine,
        audit,
        catalog: CatalogService | None = None,
    ) -> None:
        self._federation = federation
        self._spine = spine
        self._catalog = catalog
        self._audit = audit

    def procure(
        self,
        specification: RfqSpecification,
        *,
        tenant: str,
        principal: str,
        agent: str,
        idempotency_key: str,
        now: datetime,
        category: str = "",
        delivery_destination: str = "",
        risk_facts: dict[str, Any] | None = None,
        exclude_origins: frozenset[str] = frozenset(),
    ) -> tuple[SourcedOffer, StoredIntent]:
        """Source everywhere, mint the intent for the best eligible offer.

        ``exclude_origins`` is the fallback lever: a caller whose placement
        failed excludes the origin that failed and procures again — the
        idempotency key must change with the attempt, because a different
        source is a different ask.
        """
        rows = self._federation.source(
            specification, catalog=self._catalog, now=now
        )
        candidates = [
            row
            for row in rows
            if row.eligible and row.origin not in exclude_origins
        ]
        if not candidates:
            self._audit.append(
                "market.supply.exhausted",
                {
                    "tenant": tenant,
                    "category": specification.category,
                    "quantity": specification.quantity,
                    "examined": len(rows),
                    "excluded": sorted(exclude_origins),
                },
            )
            raise WrongState(
                "no eligible source can serve this specification"
            )
        chosen = candidates[0]
        facts = dict(risk_facts or {})
        # The ladder hears exactly the attestation the source carries —
        # an orchestrator never vouches for anyone.
        facts.setdefault("seller_identity_verified", chosen.seller_attested)
        stored, _ = self._spine.create_purchase_intent(
            tenant=tenant,
            principal=principal,
            agent=agent,
            offer=chosen.offer,
            idempotency_key=idempotency_key,
            now=now,
            category=category or specification.category,
            delivery_destination=delivery_destination,
            risk_facts=facts,
        )
        self._audit.append(
            "market.supply.sourced",
            {
                "tenant": tenant,
                "origin": chosen.origin,
                "offer_id": chosen.offer.offer_id,
                "total_micros": chosen.offer.total_micros,
                "alternatives": len(candidates) - 1,
                "excluded": sorted(exclude_origins),
                "intent_id": stored.intent.intent_id,
            },
        )
        return chosen, stored
