"""Multi-agent work over shared state — M6 of the memory-stack plan.

Agents cooperate through the STACK, never through each other's
transcripts (capability-web §21). Everything here rides machinery that
already stands — the durable task queue's leases, the M0 spine, M2's
episodes and derived summaries, the build ledger's seat scoreboard,
M5's context buckets — and adds exactly four disciplines:

- **The typed baton.** :class:`Handoff` carries current state refs,
  completed execution refs, evidence, unresolved bindings, and
  acceptance evaluators — a ``RunState`` excerpt, not a chat log. The
  shape is the wall: there is no transcript field to fill, every item
  is length-capped so none can be smuggled, and a handoff that
  references nothing is refused (a baton with no state attached hands
  nothing off).
- **Claims are leases.** :func:`hand_off` enqueues the baton on the
  durable queue; :func:`claim` leases it — one owner, bounded time,
  heartbeats, reclaim on death — so duplicate claims are impossible by
  the queue's own contract, not by politeness. Execution authorization
  stays where it lives: the worker control plane's SIGNED single-use
  leases (``worker/leases.py``); the queue lease here is the claim
  wall, not an execution grant.
- **Resume is a projection.** :func:`resume` rebuilds the receiving
  agent's working context from events alone — the subject's episodes
  and derived summary (M2, recomputed, never patched) plus the baton's
  verbatim unresolved items and acceptance bars. Its signature accepts
  no transcript, so "resumes from state, not chat" holds structurally.
- **Judgement is independent.** Expertise is DERIVED, never stored
  (:func:`expertise_board` — the seat scoreboard's publish/refuse
  history joined with M5's model-bucketed route posteriors, one Beta
  posterior over both evidence families, so volume beats luck), and it
  feeds ``expert_score`` routing (:func:`assign_verifier`). The agent
  that produced a deliverable never scores it: :func:`record_verdict`
  refuses a self-verdict, and :func:`resolve` refuses a resolver
  crowning their own proposal. Conflicting proposals persist SEPARATELY
  on the spine (``proposed`` — bare arrival is that state's right)
  until an evaluator or human resolves; the resolution supersedes them
  all, citing the winner.
"""

from __future__ import annotations

from typing import Sequence

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .episodes import summarize
from .knowledge.traces import TraceStore, route_node_key
from .routelearning import context_bucket

# One item in a baton is a reference or a commitment, never an essay —
# the cap that keeps a "handoff" from becoming a transcript in disguise.
MAX_ITEM_CHARS = 400

HANDOFF_KIND = "handoff"


class Handoff(BaseModel):
    """The typed baton — what one agent passes another INSTEAD of its
    conversation. Refs must resolve on durable storage; the receiving
    agent rebuilds everything else from the stack."""

    model_config = ConfigDict(frozen=True)

    tenant: str
    subject: str  # the goal/task key the work belongs to
    objective: str  # where the colleague was driving, one line
    from_agent: str
    state_refs: tuple[str, ...] = ()  # RunState / state-card refs
    completed_refs: tuple[str, ...] = ()  # finished execution refs
    evidence: tuple[str, ...] = ()  # event ids on durable storage
    unresolved: tuple[str, ...] = ()  # open bindings/questions, verbatim
    acceptance: tuple[str, ...] = ()  # the bars the work must clear

    @field_validator(
        "objective",
        "state_refs",
        "completed_refs",
        "evidence",
        "unresolved",
        "acceptance",
    )
    @classmethod
    def _bounded(cls, value):
        items = (value,) if isinstance(value, str) else value
        for item in items:
            if len(str(item)) > MAX_ITEM_CHARS:
                raise ValueError(
                    "a handoff item is a reference or a commitment, never a"
                    f" transcript — {MAX_ITEM_CHARS} chars is the wall"
                )
        return value

    @model_validator(mode="after")
    def _references_something(self):
        if not (self.state_refs or self.completed_refs or self.evidence):
            raise ValueError(
                "a handoff that references no state, no completed work, and"
                " no evidence hands nothing off"
            )
        return self


def hand_off(
    queue,
    spine,
    *,
    handoff: Handoff,
    idempotency_key: str | None = None,
) -> str:
    """Enqueue the baton and land its record on the spine; returns the
    queue task id. The spine record is the shared-state truth ("who
    handed what to whom, on what evidence"); the queue task is the
    claimable work item. Advisory memory, mandatory queue: a spine
    hiccup never loses the baton."""
    task = queue.enqueue(
        HANDOFF_KIND,
        handoff.model_dump(mode="json"),
        idempotency_key=idempotency_key,
    )
    if spine is not None:
        try:
            spine.admit(
                HANDOFF_KIND,
                (
                    f"handoff from {handoff.from_agent}: {handoff.objective}"
                    + (
                        f" — open: {'; '.join(handoff.unresolved[:3])}"
                        if handoff.unresolved
                        else ""
                    )
                ),
                scope_ids=(handoff.tenant, handoff.subject),
                verification_state="observed",
                provenance=(
                    *handoff.evidence,
                    *handoff.state_refs,
                    f"task:{task.task_id}",
                ),
                confidence=0.8,
                structured_value=handoff.model_dump(mode="json"),
                source_seat=handoff.from_agent,
            )
        except Exception:  # noqa: BLE001 - memory is advisory, never fatal
            pass
    return task.task_id


def claim(queue, *, agent: str, lease_seconds: float = 60.0, now=None):
    """Lease the next handoff for this agent: ``(task, Handoff)`` or
    ``None`` with nothing claimable. Reclaims expired leases first, so
    a colleague's death releases their baton here, in the same call the
    successor claims with. Raises on a queue whose next task is not a
    handoff — a shared-family queue is a wiring mistake to say loudly,
    not to silently absorb (the lease expires back on its own)."""
    queue.reclaim_expired(now=now)
    task = queue.lease(agent, lease_seconds=lease_seconds, now=now)
    if task is None:
        return None
    if task.kind != HANDOFF_KIND:
        raise ValueError(
            f"leased a {task.kind!r} task from the handoff queue — handoffs"
            " ride their own queue (or the caller dispatches by kind);"
            " the lease will expire back to its family"
        )
    return task, Handoff.model_validate(task.payload)


def resume(spine, *, tenant: str, subject: str, handoff: Handoff) -> dict:
    """The receiving agent's working context, from events alone.

    Recomputes the subject's summary from its standing episodes (M2's
    law: derived, extractive, never patched), then joins the baton's
    verbatim commitments. No transcript enters: the signature has
    nowhere to put one. The union of unresolved items keeps BOTH
    sources' commitments — the stack's memory and the colleague's last
    word each survive the interruption."""
    summarize(spine, tenant=tenant, subject=subject)
    summaries = spine.recall((tenant, subject), kinds=("summary",), limit=1)
    summary = (summaries[0].get("structured_value") or {}) if summaries else {}
    unresolved = list(summary.get("unresolved", []))
    for item in handoff.unresolved:
        if item not in unresolved:
            unresolved.append(item)
    return {
        "subject": subject,
        "objective": summary.get("objective") or handoff.objective,
        "latest_outcome": summary.get("latest_outcome", ""),
        "unresolved": unresolved,
        "acceptance": list(handoff.acceptance),
        "state_refs": list(handoff.state_refs),
        "completed_refs": list(handoff.completed_refs),
        "evidence": list(handoff.evidence),
        "handed_off_by": handoff.from_agent,
    }


# --------------------------------------------------------------------- #
# Expertise — derived from seat performance + trace outcomes, never     #
# stored (the projection law: rebuild-equals-read).                     #
# --------------------------------------------------------------------- #
def expert_score(
    *, published: int = 0, refused: int = 0, successes: float = 0.0, failures: float = 0.0
) -> float:
    """One Beta posterior mean over both evidence families — seat
    outcomes (published/refused) and trace outcomes — so an agent with
    one lucky publish never outranks one with a proven volume, and an
    agent with no evidence sits at the uniform 0.5, explorable but
    never presumed expert."""
    wins = float(published) + float(successes)
    losses = float(refused) + float(failures)
    return (1.0 + wins) / (2.0 + wins + losses)


def expertise_board(
    *,
    seat_rows: Sequence[dict] = (),
    store: TraceStore | None = None,
    subject: str = "",
    agents: Sequence[str] = (),
) -> list[dict]:
    """Ranked expertise, derived on every call: the seat scoreboard's
    per-model history (``BuildLedger.seat_performance`` rows, or any
    scoreboard of that shape) joined — when a subject is named — with
    the agent's route posterior in its own M5 model bucket, which is
    where per-(route, model) outcomes already accumulate. Rows carry
    their evidence volume so a caller can see WHY a score is what it
    is; ties break by name for stable routing."""
    names = list(agents) or [str(row.get("model", "")) for row in seat_rows]
    by_model = {str(row.get("model", "")): row for row in seat_rows}
    board: list[dict] = []
    for name in dict.fromkeys(names):
        seat = by_model.get(name, {})
        published = int(seat.get("published", 0))
        refused = int(seat.get("refused", 0))
        successes = failures = 0.0
        if store is not None and subject:
            # Exact bucket only: expertise attributes CREDIT, and the
            # global fallback would hand every agent the route's record.
            posterior = store.posterior(
                route_node_key(subject),
                context_bucket({"model": name}),
                fallback=False,
            )
            successes, failures = posterior.successes, posterior.failures
        board.append(
            {
                "agent": name,
                "score": expert_score(
                    published=published,
                    refused=refused,
                    successes=successes,
                    failures=failures,
                ),
                "evidence": published + refused + successes + failures,
            }
        )
    board.sort(key=lambda row: (-row["score"], row["agent"]))
    return board


def assign_verifier(
    *, producer: str, board: Sequence[dict]
) -> str | None:
    """The ``expert_score`` routing with the independence law applied:
    the best-scored agent that is NOT the producer, or None when no
    independent candidate exists — an honest 'this deliverable cannot
    be verified yet', never a self-score by default."""
    for row in board:
        if str(row.get("agent", "")) != producer:
            return str(row["agent"])
    return None


def record_verdict(
    spine,
    *,
    tenant: str,
    subject: str,
    verifier: str,
    producer: str,
    verdict: str,
    concern: str = "",
    sources: Sequence[str] = (),
) -> int:
    """One independent verdict onto the spine. Refuses a self-verdict
    in words — the ``node.review`` law generalized: the agent that
    produced a deliverable never scores it, whatever the seat."""
    if verifier == producer:
        raise ValueError(
            f"{verifier!r} produced this deliverable and cannot score it —"
            " the producer never verifies its own work"
        )
    if verdict not in ("pass", "block"):
        raise ValueError("a verdict is 'pass' or 'block', nothing softer")
    statement = f"[{subject}] {verdict} by {verifier} (producer: {producer})"
    if concern:
        statement += f" — {concern[:MAX_ITEM_CHARS]}"
    return spine.admit(
        "verdict",
        statement,
        scope_ids=(tenant, subject),
        verification_state="observed",
        provenance=tuple(sources) or (f"subject:{subject}",),
        confidence=0.8,
        structured_value={
            "verdict": verdict,
            "verifier": verifier,
            "producer": producer,
            "concern": concern[:MAX_ITEM_CHARS],
        },
        source_seat=verifier,
    )


# --------------------------------------------------------------------- #
# Conflicting proposals — separate until an evaluator or human resolves. #
# --------------------------------------------------------------------- #
def propose(
    spine,
    *,
    tenant: str,
    subject: str,
    agent: str,
    statement: str,
    value: dict | None = None,
    sources: Sequence[str] = (),
) -> int:
    """One agent's proposal for a subject — ``proposed`` is the one
    state the spine admits bare, which is exactly what a proposal is:
    a position, not evidence. Rival proposals coexist; nothing here
    supersedes a colleague's view."""
    return spine.admit(
        "proposal",
        f"[{agent}] {statement[:MAX_ITEM_CHARS]}",
        scope_ids=(tenant, subject),
        verification_state="proposed",
        provenance=tuple(sources),
        confidence=0.5,
        structured_value={"agent": agent, "value": value or {}},
        source_seat=agent,
    )


def proposals(spine, *, tenant: str, subject: str, limit: int = 10) -> list[dict]:
    """Every standing proposal for a subject, side by side — the
    disagreement is the record until a resolution closes it."""
    return spine.recall((tenant, subject), kinds=("proposal",), limit=limit)


def resolve(
    spine,
    *,
    tenant: str,
    subject: str,
    winner_memory_id: int,
    by: str,
    sources: Sequence[str] = (),
) -> int:
    """An evaluator's (or human's) resolution: one decision record,
    superseding EVERY standing proposal in the scope and citing the
    winner. Refused when the resolver authored the winning proposal —
    crowning your own position is a self-verdict with extra steps."""
    standing = proposals(spine, tenant=tenant, subject=subject)
    winner = next(
        (row for row in standing if row["memory_id"] == winner_memory_id), None
    )
    if winner is None:
        raise ValueError(
            f"memory {winner_memory_id} is not a standing proposal for"
            f" {subject!r} — resolve points at a live disagreement"
        )
    author = str((winner.get("structured_value") or {}).get("agent", ""))
    if by == author:
        raise ValueError(
            f"{by!r} proposed this position and cannot resolve in its own"
            " favor — an evaluator or another agent must"
        )
    decision = spine.admit(
        "decision",
        f"[{subject}] resolved by {by}: {winner['statement'][:MAX_ITEM_CHARS]}",
        scope_ids=(tenant, subject),
        verification_state="observed",
        provenance=tuple(sources) + (f"memory:{winner_memory_id}",),
        confidence=0.85,
        structured_value={
            "resolved_by": by,
            "winner_memory_id": int(winner_memory_id),
            "winner_agent": author,
        },
        source_seat=by,
    )
    spine.supersede_scope(
        (tenant, subject), memory_type="proposal", by=decision
    )
    return decision
