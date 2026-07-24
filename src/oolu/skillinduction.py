"""Skill induction — M4 of the memory-stack plan: the ignition.

Repeated verified subgraphs become candidate skills, and capability
starts to COMPOUND instead of repeat. The pipeline is deliberately the
humble half of capability-web §19 — mining and candidacy over the
trace corpus that already exists (`TraceStore.trace_runs`, verified
runs only: a plan the platform could not verify is not an example of
how to plan) — with promotion holding the doc's thresholds:

- support ≥ 5 verified runs containing the motif,
- across ≥ 3 DISTINCT goals (contexts) — generality, not habit,
- every step verified in every supporting run.

Candidates and promotions ride the M0 spine (``skill-candidate`` →
``skill``), provenance citing the supporting runs, supersession
carrying candidacy into promotion. What is deliberately NOT here yet,
recorded in the plan: replay-against-history and mutation testing
(`orchestrator/replay.py`) as the final promotion gate, and the
publication of a promoted skill as a marketplace node bundle — the
induction half had to exist before the audition half has anything to
audition.
"""

from __future__ import annotations

MIN_SUPPORT = 5
MIN_CONTEXTS = 3
MIN_MOTIF_LEN = 2
MAX_MOTIF_LEN = 5


def _verified_step_sequences(store) -> list[tuple[str, list[str]]]:
    """(goal, ordered verified node keys) per successful run."""
    sequences: list[tuple[str, list[str]]] = []
    for run in store.runs():
        if not getattr(run, "success", False):
            continue  # only verified runs teach — the corpus's own law
        steps = run.steps
        goal = run.goal
        keys = [
            str(step[0]) if isinstance(step, (list, tuple)) else str(step.node_key)
            for step in steps
            if (step[1] if isinstance(step, (list, tuple)) else step.ok)
        ]
        if len(keys) >= MIN_MOTIF_LEN:
            sequences.append((str(goal), keys))
    return sequences


def mine_candidates(store) -> list[dict]:
    """Frequent contiguous motifs over the verified corpus — each with
    its support, its distinct contexts, and the runs that back it."""
    counts: dict[tuple[str, ...], dict] = {}
    for goal, keys in _verified_step_sequences(store):
        seen_here: set[tuple[str, ...]] = set()
        for size in range(MIN_MOTIF_LEN, min(MAX_MOTIF_LEN, len(keys)) + 1):
            for start in range(len(keys) - size + 1):
                motif = tuple(keys[start : start + size])
                if motif in seen_here:
                    continue  # one vote per run, however often it repeats
                seen_here.add(motif)
                entry = counts.setdefault(
                    motif, {"support": 0, "contexts": set()}
                )
                entry["support"] += 1
                entry["contexts"].add(goal)
    candidates = [
        {
            "steps": list(motif),
            "support": entry["support"],
            "contexts": sorted(entry["contexts"]),
        }
        for motif, entry in counts.items()
        if entry["support"] >= 2  # candidacy is cheap; promotion is earned
    ]
    candidates.sort(key=lambda c: (-c["support"], -len(c["steps"])))
    return candidates


def induce(store, spine, *, tenant: str) -> list[int]:
    """Mine and admit candidates onto the spine (idempotent per motif:
    a re-run supersedes the motif's prior candidate with fresh counts)."""
    admitted: list[int] = []
    for candidate in mine_candidates(store):
        motif_key = "→".join(candidate["steps"])
        scope = (tenant, f"motif:{motif_key}")
        prior = spine.recall(scope, kinds=("skill-candidate",), limit=1)
        row = spine.admit(
            "skill-candidate",
            (
                f"candidate skill [{motif_key}] — support "
                f"{candidate['support']} runs across "
                f"{len(candidate['contexts'])} goals"
            ),
            scope_ids=scope,
            verification_state="observed",
            provenance=(f"trace-corpus:{candidate['support']}-runs",),
            confidence=min(1.0, 0.3 + 0.1 * candidate["support"]),
            structured_value=candidate,
            source_seat="skill.induction",
            supersedes=tuple(p["memory_id"] for p in prior),
        )
        admitted.append(row)
    return admitted


def promote(spine, *, tenant: str, motif_key: str) -> int | None:
    """Promote one candidate to ``skill`` when the thresholds hold —
    support ≥ 5, contexts ≥ 3 — superseding its candidacy. Returns the
    skill's memory id, or None with the shortfall left standing (an
    unpromoted candidate is not a failure; it is patience)."""
    scope = (tenant, f"motif:{motif_key}")
    found = spine.recall(scope, kinds=("skill-candidate",), limit=1)
    if not found:
        return None
    candidate = found[0]
    value = candidate.get("structured_value") or {}
    if (
        int(value.get("support", 0)) < MIN_SUPPORT
        or len(value.get("contexts", [])) < MIN_CONTEXTS
    ):
        return None
    return spine.admit(
        "skill",
        f"skill [{motif_key}] — verified across "
        f"{len(value['contexts'])} goals, {value['support']} runs",
        scope_ids=scope,
        verification_state="verified",
        provenance=tuple(candidate.get("provenance") or ())
        + (f"memory:{candidate['memory_id']}",),
        confidence=0.9,
        structured_value={**value, "status": "verified"},
        source_seat="skill.induction",
        supersedes=(candidate["memory_id"],),
    )


def skills_for(spine, *, tenant: str, subject_steps: list[str]) -> list[dict]:
    """The promoted skills whose motif appears IN a proposed step
    sequence — the reader route assembly consults, closing M4's loop."""
    hits: list[dict] = []
    for size in range(MIN_MOTIF_LEN, min(MAX_MOTIF_LEN, len(subject_steps)) + 1):
        for start in range(len(subject_steps) - size + 1):
            motif_key = "→".join(subject_steps[start : start + size])
            for row in spine.recall(
                (tenant, f"motif:{motif_key}"), kinds=("skill",), limit=1
            ):
                if row not in hits:
                    hits.append(row)
    return hits
