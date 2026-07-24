"""Episodic memory and summaries — M2 of the memory-stack plan.

Episodes are not a new store: they are atomic memories on the M0 spine
(``memory_type="episode"``), which is the whole point of a spine — one
table, one supersession law, one reader. This module is the episode
WRITER and the summary discipline on top:

- An episode records one coherent stretch of work verbatim: objective,
  outcome, decisions, unresolved items — provenance mandatory, because
  an episode nobody can trace is a story, not a memory.
- A summary is DERIVED, extractive, and level-scoped (execution → task
  → project; never global): open unresolved items ride VERBATIM —
  commitments survive compaction — and every summary cites the episode
  rows it compressed.
- Invalidation is read-side law: a summary older than its subject's
  newest episode NEVER serves (``current_summary`` returns None), and
  re-summarizing supersedes the stale one on the spine. Recompute,
  never patch.
"""

from __future__ import annotations


def record_episode(
    spine,
    *,
    tenant: str,
    subject: str,
    kind: str,
    objective: str,
    outcome: str,
    unresolved: tuple[str, ...] | list[str] = (),
    decisions: tuple[str, ...] | list[str] = (),
    sources: tuple[str, ...] | list[str] = (),
) -> int:
    """One stretch of work onto the spine. Episodes accumulate — only
    summaries supersede, and only each other."""
    return spine.admit(
        "episode",
        f"[{kind}] {objective} — outcome: {outcome}",
        scope_ids=(tenant, subject),
        verification_state="observed",
        provenance=tuple(sources) or (f"subject:{subject}",),
        confidence=0.9,
        structured_value={
            "kind": kind,
            "objective": objective,
            "outcome": outcome,
            "unresolved": list(unresolved),
            "decisions": list(decisions),
        },
        source_seat="episodes",
    )


def summarize(spine, *, tenant: str, subject: str, limit: int = 20) -> int | None:
    """Derive the subject's summary from its unsuperseded episodes and
    admit it, superseding the prior summary. Extractive on purpose:
    the newest objective and outcome, every OPEN unresolved item
    verbatim, decisions deduped — no model, no paraphrase, no global
    view. Returns the summary's memory id, or None with no episodes."""
    episodes = spine.recall(
        (tenant, subject), kinds=("episode",), limit=limit
    )
    if not episodes:
        return None
    newest = episodes[0]
    unresolved: list[str] = []
    decisions: list[str] = []
    outcomes: list[str] = []
    for episode in episodes:
        value = episode.get("structured_value") or {}
        outcomes.append(str(value.get("outcome", "")))
        for item in value.get("unresolved", []):
            if item and item not in unresolved:
                unresolved.append(str(item))
        for item in value.get("decisions", []):
            if item and item not in decisions:
                decisions.append(str(item))
    latest = newest.get("structured_value") or {}
    statement = (
        f"summary: {latest.get('objective', subject)} — latest outcome: "
        f"{latest.get('outcome', 'unknown')}; "
        f"{len([o for o in outcomes if o])} episodes"
    )
    if unresolved:
        statement += "; OPEN: " + "; ".join(unresolved[:5])
    prior = spine.recall((tenant, subject), kinds=("summary",), limit=1)
    summary_id = spine.admit(
        "summary",
        statement,
        scope_ids=(tenant, subject),
        verification_state="observed",
        provenance=tuple(f"memory:{e['memory_id']}" for e in episodes),
        confidence=0.8,
        structured_value={
            "objective": latest.get("objective", ""),
            "latest_outcome": latest.get("outcome", ""),
            "unresolved": unresolved,
            "decisions": decisions,
            "episode_count": len(episodes),
            "newest_episode_id": int(newest["memory_id"]),
        },
        source_seat="episodes",
        supersedes=tuple(p["memory_id"] for p in prior),
    )
    return summary_id


def current_summary(spine, *, tenant: str, subject: str) -> dict | None:
    """The subject's summary — ONLY while no newer episode exists. A
    stale summary never serves; the caller re-summarizes instead. This
    is invalidation as a read-side law, not a background job's promise."""
    found = spine.recall((tenant, subject), kinds=("summary",), limit=1)
    if not found:
        return None
    summary = found[0]
    value = summary.get("structured_value") or {}
    newest_cited = int(value.get("newest_episode_id", 0))
    episodes = spine.recall((tenant, subject), kinds=("episode",), limit=1)
    if episodes and int(episodes[0]["memory_id"]) > newest_cited:
        return None  # a newer episode invalidated it — recompute to serve
    return summary
