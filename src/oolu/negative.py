"""Negative knowledge — M3 of the memory-stack plan.

Failed work must stay useful without hardening into an unjustified
universal prohibition (capability-web §18). Failure records ride the
M0 spine (``memory_type="failure"``), dual-scoped — to their GOAL and
to their MECHANISM — with applicability conditions, reproduction
counts, and reopen conditions.

The check before commitment is deliberately graduated:

- ONE failure never blocks — the lesson rides the pack (M0) and the
  retry proceeds; a single data point is a warning, not a law.
- A REPRODUCED failure (the same mechanism, twice) blocks an identical
  retry — and the block NAMES the failure and what would make a retest
  material.
- A materially different context (a different model in the seat, a
  matched reopen condition) is ALLOWED, with the difference named —
  retests are earned by change, not by hope.

A publish resolves the goal's failure records the same way it closes
its lessons: superseded, never erased.
"""

from __future__ import annotations


def _mechanism_of(problem: str) -> str:
    """A stable mechanism key from a failure's words: the classifier
    token before the first colon when one exists, else the first words
    — enough to say 'the same way' without a model."""
    head = str(problem or "").strip().split("\n", 1)[0]
    if ":" in head:
        return head.split(":", 1)[0].strip().lower()[:80]
    return head.lower()[:80]


def record_failure(
    spine,
    *,
    tenant: str,
    subject: str,
    problem: str,
    applicability: dict | None = None,
    sources: tuple[str, ...] | list[str] = (),
    reopen_conditions: tuple[str, ...] | list[str] = (),
) -> int:
    """One scoped failure onto the spine — goal-scoped, with a
    mechanism-scoped twin for cross-goal retrieval. A recurrence of the
    same mechanism supersedes the old record with the count raised:
    reproduction is evidence, and evidence promotes."""
    mechanism = _mechanism_of(problem)
    prior = [
        r
        for r in spine.recall((tenant, subject), kinds=("failure",), limit=10)
        if (r.get("structured_value") or {}).get("mechanism") == mechanism
    ]
    reproductions = 1 + max(
        (int((r.get("structured_value") or {}).get("reproductions", 1)) for r in prior),
        default=0,
    )
    value = {
        "mechanism": mechanism,
        "failure_mode": str(problem)[:400],
        "applicability": dict(applicability or {}),
        "reproductions": reproductions,
        "reopen_conditions": [str(c) for c in reopen_conditions],
        "root_cause_state": "unknown",
    }
    provenance = tuple(sources) or (f"subject:{subject}",)
    row = spine.admit(
        "failure",
        f"failed via {mechanism} (x{reproductions}): {str(problem)[:200]}",
        scope_ids=(tenant, subject),
        verification_state="observed",
        provenance=provenance,
        confidence=min(1.0, 0.5 + 0.2 * reproductions),
        structured_value=value,
        source_seat="node.build",
        supersedes=tuple(r["memory_id"] for r in prior),
    )
    spine.admit(
        "failure",
        f"[{subject}] failed via {mechanism}: {str(problem)[:200]}",
        scope_ids=(tenant, f"mechanism:{mechanism}"),
        verification_state="observed",
        provenance=provenance,
        confidence=0.6,
        structured_value=value,
        source_seat="node.build",
    )
    return row


def negative_check(
    spine, *, tenant: str, subject: str, context: dict | None = None
) -> dict:
    """The pre-commitment verdict: ``{"blocked", "reason", "difference"}``.

    Blocked only by a REPRODUCED goal-scoped failure whose applicability
    matches the current context; any differing applicability key or a
    matched reopen condition allows the retest — with the material
    difference named, so the allowance is an argument, not a shrug."""
    context = dict(context or {})
    for record in spine.recall((tenant, subject), kinds=("failure",), limit=5):
        value = record.get("structured_value") or {}
        if int(value.get("reproductions", 1)) < 2:
            continue
        context_words = " ".join(str(v) for v in context.values()).lower()
        reopened = [
            c
            for c in value.get("reopen_conditions", [])
            if c and str(c).lower() in context_words
        ]
        if reopened:
            return {
                "blocked": False,
                "reason": "",
                "difference": f"reopen condition met: {reopened[0]}",
            }
        applicability = value.get("applicability") or {}
        differing = [
            key
            for key, known in applicability.items()
            if str(context.get(key, known)) != str(known)
        ]
        if differing:
            return {
                "blocked": False,
                "reason": "",
                "difference": (
                    "materially different from the recorded failure: "
                    + ", ".join(f"{k} changed" for k in differing)
                ),
            }
        return {
            "blocked": True,
            "reason": (
                f"this goal already failed the same way "
                f"{value.get('reproductions')} times — "
                f"{value.get('failure_mode', '')[:160]} — change something "
                "material (a different model, a revised goal) before retrying"
            ),
            "difference": "",
        }
    return {"blocked": False, "reason": "", "difference": ""}
