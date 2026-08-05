"""The node books and the vitality reading (V6).

Every node gets honest books: income read from the earnings chain
(billing entry → metering event → run binding, weighted by each
version's cleared share), costs read from the MEASURED stores — the
model spend its build and repairs booked (`model_usage_nodes`) and the
sandbox wall time its runs metered (`compute_usage`) — and a net the
vitality law judges. Nothing here estimates: an empty store reads as
zero, and zero never retires anyone (the law's floor is negative).

The VITALITY multiplier is the books' one-number face: net income,
stability, and trust, decayed by staleness, clamped hard — it shifts
selection and reward slices, never grows any pool (the rewards law),
and a bounded NEIGHBOR pull lets a strong node lift its proven
collaborators a little, never a lot.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

# The vitality law's numbers, owner-ratified (plan Part IV): a node whose
# rolling-365-day net sits below −$5 retires — after a 90-day grace age,
# because a newborn is not reaped for being new.
VITALITY_NET_FLOOR_USD = -5.0
VITALITY_GRACE_DAYS = 90
VITALITY_WINDOW_DAYS = 365

# The multiplier's hard bounds: gravity shifts choices, it never becomes
# the choice. Same posture as the reward multiplier's clamp.
VITALITY_MIN = 0.75
VITALITY_MAX = 1.25
# How many pseudo-observations a full vitality tilt is worth in the
# assembler's posterior — the proposal-strength idiom: decides thin
# history, washes out under real evidence.
VITALITY_STRENGTH = 4.0
# How much of the partners' standing a node inherits through its
# co-occurrence edges — a pull, never a tide.
VITALITY_NEIGHBOR_WEIGHT = 0.25


class NodeBooks(BaseModel):
    """One node's honest books, from durable records only."""

    model_config = ConfigDict(frozen=True)

    node_id: str
    window_days: int = VITALITY_WINDOW_DAYS
    income_micros: int = 0
    model_cost_usd: float = 0.0
    compute_cost_usd: float = 0.0
    # The owner's ad-dividend income in context — attributed per
    # PRINCIPAL by the press chain today (the per-node bridge needs
    # story→run provenance the press schema does not yet record; named
    # follow-up). Shown, not counted into this node's net.
    owner_ad_income_micros: int = 0
    age_days: float = 0.0
    days_stale: float = 0.0
    health_score: float | None = None
    trust: float = 1.0
    vitality: float = 1.0

    @property
    def income_usd(self) -> float:
        return self.income_micros / 1_000_000

    @property
    def cost_usd(self) -> float:
        return self.model_cost_usd + self.compute_cost_usd

    @property
    def net_usd(self) -> float:
        return self.income_usd - self.cost_usd


def vitality_multiplier(
    *,
    net_usd: float,
    health_score: float | None,
    trust: float,
    days_stale: float,
) -> float:
    """The bounded gravity scalar: net income (±0.15 through a tanh so a
    whale never dominates), stability (±0.05 from the verified health
    score), verified trust (+0.05 at a KYC multiplier), all decayed by
    staleness (a node nobody has run in half a year cools toward its
    floor), clamped to [VITALITY_MIN, VITALITY_MAX]."""
    base = 1.0 + 0.15 * math.tanh(net_usd / 10.0)
    if health_score is not None:
        base += 0.05 * (min(1.0, max(0.0, health_score)) - 0.5) * 2.0
    base += 0.05 * min(1.0, max(0.0, trust - 1.0))
    freshness = 0.9 + 0.1 * math.exp(-max(0.0, days_stale) / 180.0)
    return min(VITALITY_MAX, max(VITALITY_MIN, base * freshness))


def neighbor_adjusted(
    own: float,
    partner_vitalities: list[tuple[float, float]],
) -> float:
    """Fold the co-occurrence NEIGHBOR pull into a node's own vitality:
    each ``(strength, partner_vitality)`` pair — strength from
    ``links.pair_strength`` — pulls by its partner's distance from
    neutral, weighted and re-clamped. A strong node lifts its proven
    collaborators; it cannot carry a corpse, and a weak neighborhood
    cannot sink a sound node past the floor."""
    if not partner_vitalities:
        return own
    pull = sum(
        strength * (partner - 1.0)
        for strength, partner in partner_vitalities
    ) / len(partner_vitalities)
    return min(
        VITALITY_MAX, max(VITALITY_MIN, own + VITALITY_NEIGHBOR_WEIGHT * pull)
    )


class BooksReader:
    """The one composed read: books per node, vitality per node — with a
    small TTL cache so per-candidate reads during assembly stay cheap."""

    def __init__(
        self,
        *,
        registry,
        desk=None,  # WorkDesk: income + health
        model_usage=None,  # billing.ModelUsageStore: node model spend
        compute=None,  # metering.ComputeMeterStore: measured sandbox time
        trust=None,  # (node_id) -> float: the KYC multiplier
        ledger=None,  # billing.EarningsLedger: the owner's ad context line
        clock=None,
        cache_ttl_s: float = 300.0,
    ) -> None:
        self._registry = registry
        self._desk = desk
        self._model_usage = model_usage
        self._compute = compute
        self._trust = trust
        self._ledger = ledger
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ttl = cache_ttl_s
        self._cache: dict[str, tuple[float, NodeBooks]] = {}

    def books(self, node_id: str) -> NodeBooks | None:
        node = self._registry.get_node(node_id)
        if node is None:
            return None
        now = self._clock()
        since = now - timedelta(days=VITALITY_WINDOW_DAYS)
        income = 0
        health_score = None
        if self._desk is not None:
            try:
                income = self._desk.node_income_micros(node_id, since=since)
            except Exception:  # noqa: BLE001 - an empty line, never a crash
                income = 0
        model_cost = 0.0
        if self._model_usage is not None:
            try:
                model_cost = self._model_usage.node_cost(
                    node.tenant_id, node_id, months=12
                )
            except Exception:  # noqa: BLE001
                model_cost = 0.0
        compute_cost = 0.0
        newest_version_at = node.created_at
        node_keys = {f"node:{node.skill_id}"}
        for version in self._registry.list_versions(node_id):
            newest_version_at = max(newest_version_at, version.published_at)
        if self._compute is not None:
            try:
                compute_cost = sum(
                    self._compute.node_cost(key, since=since)
                    for key in node_keys
                )
            except Exception:  # noqa: BLE001
                compute_cost = 0.0
        ad_income = 0
        if self._ledger is not None:
            try:
                ad_income = sum(
                    entry.amount_micros
                    for entry in self._ledger.entries(node.noder_principal)
                    if (entry.event_id or "").startswith("ad:")
                    and entry.created_at >= since
                )
            except Exception:  # noqa: BLE001
                ad_income = 0
        trust = 1.0
        if self._trust is not None:
            try:
                trust = max(1.0, float(self._trust(node_id)))
            except Exception:  # noqa: BLE001
                trust = 1.0
        if self._desk is not None:
            try:
                version_ids = [
                    v.version_id for v in self._registry.list_versions(node_id)
                ]
                health_score = self._desk._health(version_ids).score
            except Exception:  # noqa: BLE001
                health_score = None
        age_days = max(0.0, (now - node.created_at).total_seconds() / 86_400)
        days_stale = max(
            0.0, (now - newest_version_at).total_seconds() / 86_400
        )
        books = NodeBooks(
            node_id=node_id,
            income_micros=income,
            model_cost_usd=model_cost,
            compute_cost_usd=compute_cost,
            owner_ad_income_micros=ad_income,
            age_days=age_days,
            days_stale=days_stale,
            health_score=health_score,
            trust=trust,
        )
        return books.model_copy(
            update={
                "vitality": vitality_multiplier(
                    net_usd=books.net_usd,
                    health_score=health_score,
                    trust=trust,
                    days_stale=days_stale,
                )
            }
        )

    def vitality(self, node_id: str) -> float:
        """The cached one-number read assembly leans on — TTL-bounded so
        a thousand candidate reads cost one books computation each five
        minutes per node."""
        import time

        now = time.monotonic()
        cached = self._cache.get(node_id)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1].vitality
        books = self.books(node_id)
        value = books.vitality if books is not None else 1.0
        if books is not None:
            self._cache[node_id] = (now, books)
        return value


class RetirementNotice(BaseModel):
    """What the sweep files when the law retires a node."""

    model_config = ConfigDict(frozen=True)

    node_id: str
    title: str = ""
    tenant: str = ""
    owner: str = ""
    net_usd: float = 0.0
    books: dict = Field(default_factory=dict)
