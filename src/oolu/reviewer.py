"""The publish reviewer — a second pair of eyes before a node is listed.

Phase 6 of the context-harness plan (draft → review). Birth verification
(Phase 4) proves a function EXECUTES and speaks the contract; it cannot
prove the function is the right citizen for the desk — that its declared
interface matches what the code actually consumes and produces, that it
computes from real inputs instead of smuggling plausible constants past
the mock screen, that it reuses the slot vocabulary instead of minting
synonyms that break route chaining. Those are judgement calls, and this
seat (``node.review``, seats.py) makes them: a different consultation,
under its own purpose in the meter and the books — and, because the
canonical interface made models interchangeable, possibly a different
provider than the author (the reviewer that never shares the author's
blind spots).

The reviewer is ADVISORY IN AVAILABILITY, DECISIVE IN VERDICT: a host
with no reviewer seated publishes exactly as before (the walls of
Phases 2–5 still stand), and a reviewer that cannot be reached never
blocks a build — but a seated reviewer's BLOCK is final: the build
refuses, the reason lands on the ledger as a lesson, and the next
attempt's context pack carries it. The reviewer judges; it never edits
— a reviewer that can rewrite is an author with extra steps.
"""

from __future__ import annotations

import re

REVIEW_SYSTEM_PROMPT = """\
You review a node's execution function AFTER it verified in the sandbox
and BEFORE it is published to the desk. You judge; you never rewrite.

Hold it against exactly three bars:
1. CONTRACT FIT — the declared interface is the code's truth: every
   declared input is actually read (from ./bindings.json), every
   declared output is actually emitted, nothing consumed or produced
   goes undeclared.
2. THE EXACT-VALUE RULE — the function computes from its real inputs
   (bindings, staged files, http_request); it never emits invented,
   hardcoded, or placeholder data dressed up as a computed answer.
3. SLOT-VOCABULARY REUSE — when the desk context names slots already in
   circulation for the same values, the interface reuses those exact
   names; a synonym breaks every route that would chain on it.

Answer with EXACTLY one line, then optional detail:
VERDICT: pass
or
VERDICT: block — <the one concern that matters most, in one sentence>

Block only for a real violation of the three bars. Style, taste, and
"I would have written it differently" are never grounds to block."""

# The forced-delivery schema for routers that speak structured output.
_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "block"]},
        "concern": {"type": "string"},
    },
    "required": ["verdict"],
    "additionalProperties": False,
}

_VERDICT_RE = re.compile(r"VERDICT:\s*(pass|block)\s*(?:—|-|:)?\s*(.*)", re.I)


def _review_request(goal: str, script: str, io: dict) -> str:
    import json

    return (
        f"Goal:\n{goal}\n\n"
        f"Declared interface:\n{json.dumps(io or {}, ensure_ascii=False)}\n\n"
        f"The function (already sandbox-verified):\n```python\n{script}\n```"
    )


def review_node_function(
    model, goal: str, script: str, io: dict
) -> tuple[bool, str]:
    """``(approved, concern)`` from one review consultation.

    Structured delivery when the model speaks it (the router's
    ``structured``), the VERDICT line otherwise. Every failure to
    OBTAIN a verdict — dead model, malformed reply, no verdict line —
    approves: availability must never block a build; only a reviewer's
    explicit, reasoned block does."""
    request = [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": _review_request(goal, script, io)},
    ]
    structured = getattr(model, "structured", None)
    if callable(structured):
        try:
            result = structured(request, schema=_REVIEW_SCHEMA)
            approved = str(result.get("verdict", "pass")).lower() != "block"
            return approved, str(result.get("concern", "")).strip()
        except Exception:  # noqa: BLE001 - fall through to the prose channel
            pass
    try:
        raw = model.reply(request)
    except Exception:  # noqa: BLE001 - an unreachable reviewer never blocks
        return True, ""
    match = _VERDICT_RE.search(raw or "")
    if match is None:
        return True, ""
    if match.group(1).lower() == "pass":
        return True, ""
    concern = match.group(2).strip() or "the reviewer blocked without a reason"
    return False, concern
