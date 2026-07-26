"""Negotiation within signed bounds (M2): the agent haggles, the law holds.

A negotiation agent operates on price and terms — but only inside a
:class:`NegotiationBounds` its principal signed before the first round. The
evaluator is a pure function: a proposal outside the bounds is refused
deterministically, with reasons, no matter how persuasive the other side's
model was. Exceeding the round budget exhausts the session; an exhausted or
refused session can never be agreed.

The session store counts rounds durably (an agent cannot forget how many
rounds it spent by restarting), and ``agree`` re-checks the bounds one last
time — the signed boundary is enforced at the moment of commitment, not
just during the dance.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .errors import MarketplaceError

_SCHEMA = """CREATE TABLE IF NOT EXISTS market_negotiations (
    session_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""


class OutsideBounds(MarketplaceError):
    """The proposal violates the signed boundary. Not negotiable."""


class NegotiationBounds(BaseModel):
    """What the principal signed before the agent spoke a number."""

    model_config = ConfigDict(frozen=True)

    # "buy": limit is the ceiling. "sell": limit is the floor.
    side: str  # "buy" | "sell"
    limit_price_micros: int = Field(ge=0)
    maximum_rounds: int = Field(default=5, ge=1)
    # Term names the agent may vary at all; price is always implied.
    allowed_term_variations: tuple[str, ...] = ("price",)


def proposal_violations(
    bounds: NegotiationBounds,
    *,
    price_micros: int,
    round_number: int,
    varied_terms: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Every way the proposal breaks the signed boundary. Empty = inside."""
    violations: list[str] = []
    if bounds.side == "buy" and price_micros > bounds.limit_price_micros:
        violations.append("the price is above the signed maximum")
    if bounds.side == "sell" and price_micros < bounds.limit_price_micros:
        violations.append("the price is below the signed minimum")
    if round_number > bounds.maximum_rounds:
        violations.append("the negotiation round budget is exhausted")
    for term in varied_terms:
        if term != "price" and term not in bounds.allowed_term_variations:
            violations.append(f"varying '{term}' is not within the delegation")
    return tuple(violations)


class NegotiationSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    tenant_id: str
    principal_id: str
    counterparty: str
    bounds: NegotiationBounds
    rounds_used: int = 0
    state: str = "open"  # open | agreed | exhausted | refused
    agreed_price_micros: int | None = None
    created_at: datetime


class NegotiationLedger:
    """Durable sessions: the round count survives the agent."""

    def __init__(self, conn, *, audit) -> None:
        self._conn = conn
        self._audit = audit
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def _save(self, session: NegotiationSession) -> NegotiationSession:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO market_negotiations
                   (session_id, tenant, payload_json) VALUES (?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     payload_json = excluded.payload_json""",
                (session.session_id, session.tenant_id, session.model_dump_json()),
            )
        return session

    def open(
        self,
        *,
        tenant: str,
        principal: str,
        counterparty: str,
        bounds: NegotiationBounds,
        now: datetime,
    ) -> NegotiationSession:
        session = NegotiationSession(
            session_id=uuid4().hex,
            tenant_id=tenant,
            principal_id=principal,
            counterparty=counterparty,
            bounds=bounds,
            created_at=now,
        )
        self._audit.append(
            "market.negotiation.opened",
            {
                "tenant": tenant,
                "session_id": session.session_id,
                "side": bounds.side,
                "limit_price_micros": bounds.limit_price_micros,
                "maximum_rounds": bounds.maximum_rounds,
            },
        )
        return self._save(session)

    def get(self, session_id: str, *, tenant: str) -> NegotiationSession | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM market_negotiations"
                " WHERE session_id = ? AND tenant = ?",
                (session_id, tenant),
            ).fetchone()
        if row is None:
            return None
        return NegotiationSession.model_validate_json(row["payload_json"])

    def counter(
        self,
        session_id: str,
        *,
        tenant: str,
        price_micros: int,
        varied_terms: tuple[str, ...] = (),
    ) -> NegotiationSession:
        """Spend one round on a counter-proposal the bounds allow.

        A violating proposal costs the round AND is refused — an agent
        cannot probe outside the boundary for free."""
        session = self.get(session_id, tenant=tenant)
        if session is None or session.state != "open":
            raise OutsideBounds("the negotiation is not open")
        rounds = session.rounds_used + 1
        violations = proposal_violations(
            session.bounds,
            price_micros=price_micros,
            round_number=rounds,
            varied_terms=varied_terms,
        )
        exhausted = rounds >= session.bounds.maximum_rounds
        state = "open"
        if violations:
            state = "refused" if rounds <= session.bounds.maximum_rounds else "exhausted"
        elif exhausted:
            state = "exhausted"
        updated = session.model_copy(
            update={"rounds_used": rounds, "state": state}
        )
        self._save(updated)
        self._audit.append(
            "market.negotiation.round",
            {
                "tenant": tenant,
                "session_id": session_id,
                "round": rounds,
                "price_micros": price_micros,
                "violations": list(violations),
                "state": state,
            },
        )
        if violations:
            raise OutsideBounds("; ".join(violations))
        return updated

    def agree(
        self, session_id: str, *, tenant: str, price_micros: int
    ) -> NegotiationSession:
        """Commit to a final price — re-checked against the signed bounds
        at the moment of commitment. An exhausted, refused, or violating
        agreement never happens."""
        session = self.get(session_id, tenant=tenant)
        if session is None or session.state not in ("open", "exhausted"):
            raise OutsideBounds("the negotiation cannot be agreed")
        violations = proposal_violations(
            session.bounds, price_micros=price_micros, round_number=1
        )
        if violations:
            self._audit.append(
                "market.negotiation.refused",
                {
                    "tenant": tenant,
                    "session_id": session_id,
                    "price_micros": price_micros,
                    "violations": list(violations),
                },
            )
            raise OutsideBounds("; ".join(violations))
        updated = session.model_copy(
            update={"state": "agreed", "agreed_price_micros": price_micros}
        )
        self._save(updated)
        self._audit.append(
            "market.negotiation.agreed",
            {
                "tenant": tenant,
                "session_id": session_id,
                "price_micros": price_micros,
            },
        )
        return updated
