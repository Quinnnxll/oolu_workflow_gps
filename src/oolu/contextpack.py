"""The build context pack — push the right context into the seat.

Phase 3 of the context-harness plan (docs/context-harness-plan.md). The
node author used to write nearly blind: the one-shot path saw only the
goal sentence, and the agentic path could PULL context through tools it
had to think to call. The frontier coding harnesses do the opposite —
they compile a curated map of the workspace into every request. This
module is that compiler for the ``node.build`` seat:

1. the **slot vocabulary** in circulation (routes chain on exact names;
   a synonym breaks the chain),
2. the **route position** — recent verified output shapes of the
   upstream nodes the goal names, so downstream code parses the shape
   that ACTUALLY arrives, not an imagined one (the bench's #1 silent
   failure),
3. **similar node contracts**, so conventions transfer,
4. **verified example functions** retrieved from the desk's own drawers
   — few-shot from code that has already earned trust by execution,
5. **lessons** (error patterns worth not repeating).

Retrieval is lexical (token-overlap cosine — deterministic, offline,
dependency-free); the ranking seam is what Phase 5's embedding index
replaces, not the pack shape. The budget is enforced with the Phase 2
token seam and the spec's compaction order: the verbatim classes (slot
vocabulary, upstream shapes) survive; examples compress first, then
extra contracts, then lessons — and every drop is RECORDED, because a
silently truncated pack reads as "covered everything" when it didn't.

The pack is plain labeled text prepended to the request, never a second
system prompt: the frozen system contract keeps its cache breakpoint,
and the pack rides in the volatile region where per-goal content
belongs.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass

from .providers.tokens import estimate_tokens

_WORD_RE = re.compile(r"[a-z0-9]+")


def _bag(text: str) -> Counter:
    return Counter(_WORD_RE.findall(str(text or "").lower()))


def similarity(a: str, b: str) -> float:
    """Token-overlap cosine between two texts — 0.0 (disjoint) to 1.0
    (identical bags). Deterministic and cheap; the retrieval floor the
    embedding index will one day raise, behind the same call."""
    bag_a, bag_b = _bag(a), _bag(b)
    if not bag_a or not bag_b:
        return 0.0
    dot = sum(count * bag_b[word] for word, count in bag_a.items())
    if dot == 0:
        return 0.0
    norm = math.sqrt(sum(c * c for c in bag_a.values())) * math.sqrt(
        sum(c * c for c in bag_b.values())
    )
    return dot / norm


@dataclass(frozen=True)
class NodeExample:
    """One verified node function offered as few-shot: the contract card
    (the shape ``_author_catalog`` emits) plus the drawer's script."""

    card: dict
    script: str
    score: float = 0.0


@dataclass(frozen=True)
class ContextPack:
    """The compiled block plus its own audit: what rode, what was
    dropped and why, and the tokens it occupies — the per-call trace the
    plan's observability section starts from."""

    text: str
    included: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    tokens: int = 0

    @property
    def empty(self) -> bool:
        return not self.text


def _slot_vocabulary(catalog: list[dict]) -> list[str]:
    """Every slot name in circulation, with its type — sorted, deduped;
    the exact names a chainable interface must reuse."""
    seen: dict[str, str] = {}
    for node in catalog:
        for side in ("consumes", "produces"):
            for slot in node.get(side) or []:
                name = str(slot.get("name", "")).strip()
                if name and name not in seen:
                    seen[name] = str(slot.get("type", "str"))
    return [f"{name}({kind})" for name, kind in sorted(seen.items())]


def _contract_line(node: dict) -> str:
    def side(items):
        return (
            "["
            + ", ".join(
                f"{s.get('name')}:{s.get('type', 'str')}" for s in items or []
            )
            + "]"
        )

    return (
        f"- {node.get('title') or node.get('node_id')}: consumes "
        f"{side(node.get('consumes'))} produces {side(node.get('produces'))}"
        f" — {str(node.get('goal') or '').strip()}"
    )


@dataclass
class ContextPackCompiler:
    """Compiles one budgeted pack per build call.

    ``window`` is the answering model's context window; the pack takes
    at most ``max_share`` of it (the spec's code-or-artifacts
    allocation), leaving the rest for the system contract, the request,
    the tool transcript, and the response reserve."""

    window: int = 32_000
    max_share: float = 0.30
    max_examples: int = 3
    max_contracts: int = 8

    def budget_tokens(self) -> int:
        return max(0, int(self.window * self.max_share))

    # ------------------------------------------------------------------ #
    def compile(
        self,
        goal: str,
        *,
        catalog: list[dict] | None = None,
        examples: list[NodeExample] | None = None,
        upstream: list[dict] | None = None,
        lessons: list[str] | None = None,
    ) -> ContextPack:
        """The pack for one goal. ``catalog`` is the desk's contract
        cards; ``examples`` verified functions (pre-read scripts);
        ``upstream`` entries of ``{"node_id","title","outputs"}`` for
        nodes the goal sits downstream of; ``lessons`` short strings.
        Everything is optional — an empty desk compiles an empty pack,
        and the author simply works as before."""
        catalog = list(catalog or [])
        upstream = list(upstream or [])
        lessons = [str(item).strip() for item in (lessons or []) if str(item).strip()]
        ranked_examples = sorted(
            (examples or []), key=lambda e: e.score, reverse=True
        )[: self.max_examples]
        ranked_contracts = sorted(
            catalog,
            key=lambda node: similarity(
                goal, f"{node.get('title', '')} {node.get('goal', '')}"
            ),
            reverse=True,
        )[: self.max_contracts]

        included: list[str] = []
        excluded: list[str] = []
        budget = self.budget_tokens()

        # Verbatim classes first — these survive compaction whole.
        head: list[str] = []
        vocabulary = _slot_vocabulary(catalog)
        if vocabulary:
            head.append(
                "Slot vocabulary in circulation (REUSE these exact names"
                " when the node consumes or produces the same value): "
                + ", ".join(vocabulary)
            )
            included.append("slot-vocabulary")
        for shape in upstream:
            outputs = shape.get("outputs")
            if not outputs:
                continue
            head.append(
                f"Upstream node {shape.get('title') or shape.get('node_id')}"
                " — recent VERIFIED outputs (write code that parses this"
                " exact shape):\n"
                + json.dumps(outputs, ensure_ascii=False, default=str)[:2000]
            )
            included.append(f"upstream:{shape.get('node_id')}")

        # Compressible classes, in drop order (last dropped first).
        contract_lines = [_contract_line(node) for node in ranked_contracts]
        example_blocks: list[tuple[str, str]] = []
        for example in ranked_examples:
            card = example.card
            if not example.script:
                continue
            example_blocks.append(
                (
                    f"example:{card.get('node_id')}",
                    f"--- verified function: {card.get('title')}"
                    f" ({_contract_line(card)[2:]}) ---\n"
                    f"```python\n{example.script}\n```",
                )
            )

        def render(
            contracts: list[str], blocks: list[tuple[str, str]], notes: list[str]
        ) -> str:
            sections: list[str] = []
            if head:
                sections.extend(head)
            if contracts:
                sections.append(
                    "Similar node contracts on this desk:\n" + "\n".join(contracts)
                )
            if blocks:
                sections.append(
                    "Verified example functions (style and contract"
                    " reference — the request below is DIFFERENT work):\n"
                    + "\n".join(text for _key, text in blocks)
                )
            if notes:
                sections.append("Lessons from earlier failures:\n- " + "\n- ".join(notes))
            if not sections:
                return ""
            return (
                "=== Desk context (reference for the request below) ===\n"
                + "\n\n".join(sections)
                + "\n=== End of desk context ==="
            )

        contracts = list(contract_lines)
        blocks = list(example_blocks)
        notes = list(lessons)
        text = render(contracts, blocks, notes)
        # The spec's compaction order: examples compress first (lowest
        # score first — the list is ranked best-first), then contracts
        # beyond the top three, then lessons. The verbatim head is never
        # dropped; a pack that cannot even fit the head ships the head
        # and says so.
        while text and estimate_tokens(text) > budget:
            if blocks:
                key, _dropped = blocks.pop()
                excluded.append(f"{key} (budget)")
            elif len(contracts) > 3:
                contracts.pop()
                excluded.append("contract (budget)")
            elif notes:
                notes.pop()
                excluded.append("lesson (budget)")
            else:
                break
            text = render(contracts, blocks, notes)

        included.extend(key for key, _text in blocks)
        if contracts:
            included.append(f"contracts:{len(contracts)}")
        if notes:
            included.append(f"lessons:{len(notes)}")
        return ContextPack(
            text=text,
            included=tuple(included),
            excluded=tuple(excluded),
            tokens=estimate_tokens(text),
        )


def compose_build_request(
    goal: str, *, demonstrated: list[str] | None = None, context: str = ""
) -> str:
    """The one user-content shape both authoring paths send: the desk
    context (when any) first, the request last — nearest the model's
    attention — and the demonstrated-steps block exactly as before.
    With no context and no demonstration this IS the bare goal, so
    every existing caller and test reads unchanged."""
    request = goal
    if demonstrated:
        numbered = "\n".join(
            f"{i}. {step}" for i, step in enumerate(demonstrated, start=1)
        )
        request = (
            f"{goal}\n\n"
            "The user DEMONSTRATED this procedure step by step — imitate "
            "it exactly. The numbered steps below ARE the plan: write the "
            "function that performs them in this order; do not invent a "
            "different approach. Lines marked (observed: …) are execution "
            "logs recorded while they demonstrated.\n"
            f"{numbered}"
        )
    if not context:
        return request
    return f"{context}\n\nREQUEST:\n{request}"
