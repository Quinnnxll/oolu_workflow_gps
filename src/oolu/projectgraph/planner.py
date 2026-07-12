"""A model in the planner's seat — proposing, never committing.

The industrial spec's Phase 5 (a frontier model as orchestrator), sized
to what the vertical actually is: the model reads the graph, offers
structured steps, and EVERYTHING it offers goes through the same doors
as anyone else's work — the transaction kernel for truth, the judged
CAD hand for geometry. It cannot commit, cannot skip the evaluator,
cannot touch anything the bench does not expose. The loop is the
irreducible production loop with the model as P:

    the model proposes a step        (one fenced JSON verb)
    the runtime executes it          (kernel / judged CAD hand)
    the result returns as evidence   (verdicts, reasons, measurements)

Rejections come back in words — "stale", "outside the wall", the exact
broken postcondition — so a capable model can diagnose and repair: the
spec's most valuable trajectory (fail -> diagnose -> repair -> verify),
produced live. An incapable one babbles, is told the protocol once,
and is cut off; the audition report says "not fit", never a stack
trace.

The model seam is the one OoLu already speaks everywhere:
``model.reply(messages: list[dict]) -> str`` — so the desktop's
configured brain (subscription, own key, or a local server) can occupy
the seat unchanged, and CI auditions scripted stand-ins.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from pydantic import ValidationError

from ..skills.models import ActionEvent, Postcondition
from .models import GraphObject, PatchOp, ProposalResult

# The frozen protocol prompt — the cacheable prefix of every turn.
PLANNER_SYSTEM_PROMPT = """\
You are the planning seat of an engineering system. You NEVER change \
anything yourself: you offer one step at a time, and the system's \
transaction kernel and verified tool runtime carry it out and report \
back. Rejections and failed checks come back with reasons — read them, \
diagnose, and repair.

Reply with EXACTLY ONE fenced ```json block holding a single object:

- {"verb": "read", "object_id": "..."} — the object's current truth \
(free).
- {"verb": "propose", "reason": "...", "patch": [{"op": "set", \
"object_id": "...", "base_revision": N, "pointer": "parameters/...", \
"old_value": ..., "new_value": ...}]} — ops may also be "create" \
(carrying "object"), "append" (pointer "evidence" or "relations", \
"new_value"), or "supersede". Every op declares the revision it \
reasoned against.
- {"verb": "run_cad", "operation": "build"|"assemble", "parameters": \
{...}, "postconditions": [{"name": "...", "pointer": "...", "op": \
"<=", "value": ...}]} — geometry is measured, and unmet postconditions \
fail the run.
- {"verb": "done"} — when the work is verified and advanced.

No prose outside the block."""

_FENCED_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_SPAN_RE = re.compile(r"\{.*\}", re.DOTALL)

# How many consecutive unusable replies end the audition — the protocol
# is stated up front and restated once; a seat is not a tutoring job.
MAX_JUNK = 2


class GraphBench(Protocol):
    """The identical tools a planner seat gets — Level B's ``Bench``."""

    def read(self, object_id: str) -> GraphObject | None: ...

    def propose(self, reason: str, patch: list[PatchOp]) -> ProposalResult: ...

    def run_cad(self, action: ActionEvent): ...


def parse_step(text: str | None) -> dict[str, Any] | None:
    """One step object out of a completion, tolerating prose and fences.
    Anything unusable is None — unreadable planning is no planning."""
    if not text:
        return None
    candidates = [text.strip()]
    fenced = _FENCED_RE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    span = _SPAN_RE.search(text)
    if span:
        candidates.append(span.group(0))
    for blob in candidates:
        try:
            data = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("verb"), str):
            return data
    return None


class ModelPlanner:
    """A Level B contender backed by any ``model.reply`` brain.

    Callable with the bench, like every contender. ``brief`` is the
    task in words; ``bootstrap_ids`` are read (free) up front so the
    first turn already sees the world's current truth."""

    def __init__(
        self,
        model,
        *,
        brief: str,
        bootstrap_ids: tuple[str, ...] = (),
        max_turns: int = 24,
    ) -> None:
        self._model = model
        self._brief = brief
        self._bootstrap = bootstrap_ids
        self._max_turns = max_turns

    def __call__(self, bench: GraphBench) -> None:
        world = {
            object_id: self._object_view(bench.read(object_id))
            for object_id in self._bootstrap
        }
        messages: list[dict[str, str]] = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": self._brief
                + "\n\nCurrent truth:\n"
                + json.dumps(world, indent=1, default=str),
            },
        ]
        junk = 0
        for _turn in range(self._max_turns):
            raw = self._reply(messages)
            messages.append({"role": "assistant", "content": raw or ""})
            step = parse_step(raw)
            if step is None:
                junk += 1
                if junk > MAX_JUNK:
                    return  # not fit — the report will say so
                result: dict[str, Any] = {
                    "error": "unreadable step — reply with exactly one "
                    "fenced json object as instructed"
                }
            else:
                junk = 0
                if step.get("verb") == "done":
                    return
                result = self._execute(bench, step)
            messages.append(
                {"role": "user", "content": json.dumps(result, default=str)}
            )
            self._trim(messages)

    # ------------------------------------------------------------------ #
    def _reply(self, messages: list[dict[str, str]]) -> str | None:
        try:
            return self._model.reply(messages)
        except Exception:  # noqa: BLE001 - a dead model is an ended audition
            return None

    @staticmethod
    def _trim(messages: list[dict[str, str]], keep: int = 12) -> None:
        """The protocol and the world brief stay; the middle of a long
        exchange falls away — the seat plans forward, not nostalgically."""
        head, tail = messages[:2], messages[2:]
        if len(tail) > keep:
            del tail[: len(tail) - keep]
        messages[:] = head + tail

    @staticmethod
    def _object_view(obj: GraphObject | None) -> dict[str, Any] | None:
        if obj is None:
            return None
        return obj.model_dump(
            mode="json",
            include={
                "object_id",
                "path",
                "type",
                "revision",
                "status",
                "parameters",
                "constraints",
                "evidence",
            },
        )

    def _execute(self, bench: GraphBench, step: dict[str, Any]) -> dict[str, Any]:
        """One verb through the bench — every refusal answered in words
        the model can act on, never an exception."""
        verb = step.get("verb")
        try:
            if verb == "read":
                return {
                    "object": self._object_view(
                        bench.read(str(step.get("object_id") or ""))
                    )
                }
            if verb == "propose":
                patch = [
                    PatchOp.model_validate(op)
                    for op in (step.get("patch") or [])
                ]
                result = bench.propose(str(step.get("reason") or ""), patch)
                return result.model_dump(mode="json")
            if verb == "run_cad":
                action = ActionEvent(
                    correlation_id="planner",
                    adapter="cad",
                    operation=str(step.get("operation") or ""),
                    parameters=dict(step.get("parameters") or {}),
                    postconditions=[
                        Postcondition.model_validate(p)
                        for p in (step.get("postconditions") or [])
                    ],
                )
                outcome = bench.run_cad(action)
                return {
                    "status": outcome.status.value,
                    "error": outcome.error,
                    "evidence": outcome.evidence,
                }
            return {
                "error": f"unknown verb '{verb}' — read, propose, run_cad, "
                "or done"
            }
        except ValidationError as exc:
            return {"error": f"malformed step: {exc}"}
