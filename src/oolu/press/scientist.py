"""The poll floor's social scientist (A3.1) — patterns, on purpose.

Most members choose what they like, for fun — and the fun stays exactly
as it was. But the Poll agent is no longer only a croupier: it holds a
small registry of STANDING HYPOTHESES about how this community reads —
typed conjectures over signals a pair's two sides objectively differ
in, never a model's invention:

- **evidence** — are pieces with attached media preferred over words
  alone?
- **depth** — are longer, fuller tellings preferred over brief ones?
- **freshness** — is the newer telling preferred over the earlier one?

The science bends only what honesty allows it to bend:

- **Pairs are designed, not just dealt.** When the floor mines a new
  pair, the scientist chooses — among honestly comparable candidates —
  the pair that DISCRIMINATES on the least-evidenced hypothesis: one
  side affirms it, the other denies it, so every vote is also a
  measurement. A pair that tests nothing is still served before an
  empty answer; the fun never waits on the science.
- **Only decided, k-anonymous comparisons count.** A pair enters the
  evidence when its votes reach the K_FLOOR and a majority stands —
  pair-level counts only, never a voter's choice.
- **Findings are reported when they are worth sharing** — a clear
  pattern (the floor leans one way) reads like a news brief; a genuine
  split with real evidence is named a debate worth having. In between,
  the scientist keeps researching and says nothing. A finding is
  reported ONCE per verdict — the thread is a colleague, not a ticker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

from .contributions import ContentContribution
from .polls import K_FLOOR, PollPair

# A hypothesis needs this many decided, discriminating comparisons
# before any verdict is worth words.
MIN_DECIDED = 3
# The pattern bar: at or beyond this share (either way) the floor has
# leaned; inside the debate band it is honestly split.
PATTERN_SHARE = 0.7
DEBATE_LOW, DEBATE_HIGH = 0.4, 0.6
# Depth discriminates when one side out-writes the other by this factor;
# freshness when one side is this much newer.
DEPTH_RATIO = 1.5
FRESHNESS_GAP = timedelta(hours=48)


@dataclass(frozen=True)
class Hypothesis:
    key: str
    question: str  # the conjecture, in words a member can debate


HYPOTHESES: dict[str, Hypothesis] = {
    h.key: h
    for h in (
        Hypothesis(
            "evidence",
            "Do members here prefer pieces that SHOW something — a "
            "photo, a clip, a sound — over words alone?",
        ),
        Hypothesis(
            "depth",
            "Do members here prefer the longer, fuller telling over "
            "the brief one?",
        ),
        Hypothesis(
            "freshness",
            "Do members here prefer the newer telling over the "
            "earlier one?",
        ),
    )
}


def discriminates(
    key: str, left: ContentContribution, right: ContentContribution
) -> str | None:
    """Which side AFFIRMS the hypothesis — "left", "right", or None when
    this pair cannot tell the hypothesis anything."""
    if key == "evidence":
        if bool(left.media) == bool(right.media):
            return None
        return "left" if left.media else "right"
    if key == "depth":
        if len(right.body) and len(left.body) / len(right.body) >= DEPTH_RATIO:
            return "left"
        if len(left.body) and len(right.body) / len(left.body) >= DEPTH_RATIO:
            return "right"
        return None
    if key == "freshness":
        gap = left.created_at - right.created_at
        if abs(gap) < FRESHNESS_GAP:
            return None
        return "left" if gap > timedelta(0) else "right"
    return None


def discriminating_keys(
    left: ContentContribution, right: ContentContribution
) -> list[str]:
    return [k for k in HYPOTHESES if discriminates(k, left, right)]


def choose_pair(
    candidates: list[tuple[ContentContribution, ContentContribution]],
    *,
    decided_counts: dict[str, int],
) -> tuple[ContentContribution, ContentContribution]:
    """The strategic deal: among honestly comparable candidates, the
    pair testing the LEAST-evidenced hypothesis is served first — every
    vote becomes a measurement. A floor with nothing discriminating
    still deals the first candidate: the fun never waits."""
    best = None
    best_need = -1.0
    for a, b in candidates:
        keys = discriminating_keys(a, b)
        if not keys:
            continue
        # How much the hypotheses this pair tests still NEED evidence —
        # fewer decided comparisons = greater need.
        need = max(
            1.0 / (1 + decided_counts.get(key, 0)) for key in keys
        )
        if need > best_need:
            best, best_need = (a, b), need
    return best if best is not None else candidates[0]


@dataclass(frozen=True)
class Finding:
    hypothesis: str
    question: str
    verdict: str  # "affirmed" | "refuted" | "debate"
    support: int  # decided pairs where the majority took the affirming side
    against: int
    decided: int

    def report(self) -> str:
        """The field note — a news brief when a pattern holds, a debate
        statement when the floor is honestly split."""
        if self.verdict == "debate":
            return (
                "Field note from the poll floor — a debate worth having. "
                f"{self.question} The floor is split: {self.support} "
                f"decided comparison{'s' if self.support != 1 else ''} "
                f"said yes, {self.against} said no. Argue it out."
            )
        leaning = "Yes" if self.verdict == "affirmed" else "No"
        return (
            "Field note from the poll floor — a pattern holds. "
            f"{self.question} {leaning}: {self.support} of "
            f"{self.decided} decided comparisons leaned that way. "
            "(Pair-level counts only — no one's vote is visible.)"
        )


def judge(
    pairs: list[PollPair],
    *,
    counts_of: Callable[[str], dict[str, int]],
    contribution_of: Callable[[str], ContentContribution | None],
) -> tuple[list[Finding], dict[str, int]]:
    """Every hypothesis judged over the DECIDED floor: pairs whose votes
    reached the K_FLOOR with a standing majority. Returns the findings
    worth words (pattern or debate, at MIN_DECIDED evidence) and the
    decided-count book the strategic deal reads."""
    support: dict[str, int] = {k: 0 for k in HYPOTHESES}
    against: dict[str, int] = {k: 0 for k in HYPOTHESES}
    for pair in pairs:
        counts = counts_of(pair.pair_id)
        total = counts.get("left", 0) + counts.get("right", 0)
        if total < K_FLOOR or counts["left"] == counts["right"]:
            continue
        winner = "left" if counts["left"] > counts["right"] else "right"
        left = contribution_of(pair.left.contribution_id)
        right = contribution_of(pair.right.contribution_id)
        if left is None or right is None:
            continue  # an unpublished side leaves the comparison out
        for key in HYPOTHESES:
            affirming = discriminates(key, left, right)
            if affirming is None:
                continue
            if winner == affirming:
                support[key] += 1
            else:
                against[key] += 1
    findings: list[Finding] = []
    decided_counts: dict[str, int] = {}
    for key, hypothesis in HYPOTHESES.items():
        decided = support[key] + against[key]
        decided_counts[key] = decided
        if decided < MIN_DECIDED:
            continue
        share = support[key] / decided
        if share >= PATTERN_SHARE:
            verdict = "affirmed"
        elif share <= 1 - PATTERN_SHARE:
            verdict = "refuted"
        elif DEBATE_LOW <= share <= DEBATE_HIGH:
            verdict = "debate"
        else:
            continue  # leaning but not yet worth words — keep researching
        findings.append(
            Finding(
                hypothesis=key,
                question=hypothesis.question,
                verdict=verdict,
                support=support[key],
                against=against[key],
                decided=decided,
            )
        )
    return findings, decided_counts


def strategist(polls_store, contributions):
    """The scientist's deal, curried over the stores — what PollDesk
    wires as its ``strategist``: read the decided-evidence book, then
    choose the candidate pair that measures the most."""

    def deal(candidates):
        tenant = candidates[0][0].tenant_id
        pairs = polls_store.all_pairs(tenant=tenant)
        _, decided = judge(
            pairs,
            counts_of=lambda pid: polls_store.counts(pid, tenant=tenant),
            contribution_of=lambda cid: contributions.get(cid, tenant=tenant),
        )
        return choose_pair(candidates, decided_counts=decided)

    return deal


_FINDINGS_SCHEMA = """CREATE TABLE IF NOT EXISTS poll_findings (
    tenant_id TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    verdict TEXT NOT NULL,
    support INTEGER NOT NULL,
    against INTEGER NOT NULL,
    decided INTEGER NOT NULL,
    reported_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, hypothesis)
)"""


class FindingStore:
    """What the scientist already said — a finding is reported once per
    VERDICT: fresh evidence that flips or opens a verdict speaks again;
    the same conclusion twice is a ticker, not a colleague."""

    def __init__(self, conn, *, clock: Callable[[], datetime] | None = None) -> None:
        self._conn = conn
        self._clock = clock or (lambda: datetime.now(UTC))
        with self._conn.transaction() as db:
            db.execute(_FINDINGS_SCHEMA)

    def note(self, *, tenant: str, finding: Finding) -> bool:
        """Record the finding; True when its verdict is NEWS (new
        hypothesis decided, or the verdict changed) — the caller's cue
        to report it in the thread."""
        with self._conn.transaction() as db:
            row = db.execute(
                "SELECT verdict FROM poll_findings"
                " WHERE tenant_id = ? AND hypothesis = ?",
                (tenant, finding.hypothesis),
            ).fetchone()
            changed = row is None or str(row["verdict"]) != finding.verdict
            db.execute(
                """INSERT INTO poll_findings
                     (tenant_id, hypothesis, verdict, support, against,
                      decided, reported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tenant_id, hypothesis) DO UPDATE SET
                     verdict = excluded.verdict,
                     support = excluded.support,
                     against = excluded.against,
                     decided = excluded.decided,
                     reported_at = excluded.reported_at""",
                (
                    tenant,
                    finding.hypothesis,
                    finding.verdict,
                    finding.support,
                    finding.against,
                    finding.decided,
                    self._clock().isoformat(),
                ),
            )
        return changed
