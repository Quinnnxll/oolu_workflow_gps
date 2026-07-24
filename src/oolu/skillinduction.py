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
carrying candidacy into promotion. The audition half lives below:
``replay_gate`` (replay against the corpus as it stands, plus the
closedness and order-significance mutations) is the final bar, and
``publish_skill`` turns a promoted, gate-passing skill into a
marketplace bundle through an injected contribute door. A promotion
that fails the gate writes an M3 failure record — the chain
reaction's exhaust is fuel, never silence.
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


# ---------------------------------------------------------------------- #
# The final promotion gate — replay against history, mutation testing.    #
# ---------------------------------------------------------------------- #
def replay_gate(store, *, steps: list[str]) -> dict:
    """The deterministic form of capability-web §19's replay-and-mutate,
    over the same corpus ``orchestrator/replay.py`` audits planners on:

    - **Replay against history:** the motif is re-measured over the
      FULL corpus as it stands now (including runs recorded since
      candidacy), and its contradiction rate — runs where the motif's
      steps all ran but the run still FAILED — must stay within the
      doc's 5% unexplained-failure budget.
    - **Mutation — closedness:** a longer motif with the same support
      means the candidate is a fragment riding its parent's coattails;
      the parent promotes, the fragment waits.
    - **Mutation — order significance:** every adjacent swap must be
      strictly LESS supported, or the "sequence" is a bag with an
      accidental ordering, not a procedure.

    Returns ``{"passed": bool, "reasons": [...]}`` — every failed bar
    named, because a gate that says only "no" teaches nothing."""
    motif = tuple(str(s) for s in steps)
    reasons: list[str] = []

    def support_of(target: tuple[str, ...], sequences) -> int:
        count = 0
        for _goal, keys in sequences:
            for start in range(len(keys) - len(target) + 1):
                if tuple(keys[start : start + len(target)]) == target:
                    count += 1
                    break
        return count

    verified = _verified_step_sequences(store)
    contradicted = 0
    appearances = 0
    for run in store.runs():
        keys = [
            str(s[0]) if isinstance(s, (list, tuple)) else str(s.node_key)
            for s in run.steps
        ]
        present = any(
            tuple(keys[i : i + len(motif)]) == motif
            for i in range(max(0, len(keys) - len(motif) + 1))
        )
        if present:
            appearances += 1
            if not getattr(run, "success", False):
                contradicted += 1
    if appearances and contradicted / appearances > 0.05:
        reasons.append(
            f"contradiction rate {contradicted}/{appearances} exceeds the"
            " 5% unexplained-failure budget"
        )
    base = support_of(motif, verified)
    step_keys = sorted({k for _g, keys in verified for k in keys})
    for extra in step_keys:
        for longer in ((extra,) + motif, motif + (extra,)):
            if support_of(longer, verified) >= base > 0:
                reasons.append(
                    f"not closed: {'→'.join(longer)} has equal support —"
                    " the fragment waits for its parent"
                )
                break
        if reasons and reasons[-1].startswith("not closed"):
            break
    for i in range(len(motif) - 1):
        swapped = list(motif)
        swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
        if support_of(tuple(swapped), verified) >= base > 0:
            reasons.append(
                f"order-insignificant at step {i + 1}: the adjacent swap"
                " is equally supported — a bag, not a procedure"
            )
            break
    return {"passed": not reasons, "reasons": reasons}


def publish_skill(
    spine, store, *, tenant: str, motif_key: str, contribute
) -> dict:
    """Publication — a promoted skill becomes a marketplace node bundle
    through the injected ``contribute`` door (the gateway/nodeplace owns
    the actual listing walls). Refuses in words without a promoted
    skill or past a failed replay gate — and a failed gate writes an
    M3 failure record scoped to the motif, so the false promotion is
    stored as negative knowledge instead of vanishing into a refusal
    string. Success records ``skill-published`` on the spine citing
    both the skill and the listing."""
    scope = (tenant, f"motif:{motif_key}")
    found = spine.recall(scope, kinds=("skill",), limit=1)
    if not found:
        return {"published": False, "reason": "no promoted skill for this motif"}
    skill = found[0]
    steps = list((skill.get("structured_value") or {}).get("steps", []))
    verdict = replay_gate(store, steps=steps)
    if not verdict["passed"]:
        from .negative import record_failure  # local: M3 is the exhaust pipe

        record_failure(
            spine,
            tenant=tenant,
            subject=f"motif:{motif_key}",
            problem="replay gate: " + "; ".join(verdict["reasons"]),
            sources=(f"memory:{skill['memory_id']}",),
            reopen_conditions=("corpus grew",),
        )
        return {
            "published": False,
            "reason": "replay gate failed: " + "; ".join(verdict["reasons"]),
        }
    manifest = {
        "kind": "skill-bundle",
        "motif": motif_key,
        "steps": steps,
        "support": (skill.get("structured_value") or {}).get("support"),
        "contexts": (skill.get("structured_value") or {}).get("contexts"),
        "evidence": list(skill.get("provenance") or []),
    }
    listing = contribute(manifest)
    spine.admit(
        "skill-published",
        f"skill [{motif_key}] published as a marketplace bundle",
        scope_ids=scope,
        verification_state="verified",
        provenance=(f"memory:{skill['memory_id']}", f"listing:{listing}"),
        confidence=0.95,
        structured_value=manifest,
        source_seat="skill.induction",
    )
    return {"published": True, "listing": listing, "manifest": manifest}
