"""Adapter synthesis — pour the code that bridges a near-miss junction (W3).

The survey found two nodes the type system ALMOST joins. Two shapes:

  * ``mappable`` — same value_type and role, a DIFFERENT NAME: a pure
    rename. Poured as a DETERMINISTIC TEMPLATE — byte-stable, no model in
    the loop — and still earned by execution: even a template must run
    clean against the producer's real sampled value with the consumer's
    port checked before it becomes an edge.
  * ``convertible`` — the SAME NAME at a different value_type: a shape
    change. The MODEL authors the conversion, and — like every function
    OoLu synthesizes — it earns its place by EXECUTION, not by the
    model's say-so: screened, then verified against the producer's real
    sampled value with the consumer's port checked, ≤2 repair rounds.

A candidate that never passes verify+repair creates NO edge (the caller
files an M3 note so the Paver never grinds the same junction). A
candidate that passes lands as a real citizen through the same doors any
node passes — the coding-agent half that breaks the exact-name wall,
kept honest by the same verify-by-execution gate.

The synthesizer is pure over injected ports (``verify`` — the F0
``verify_function`` seam — and, for shape adapters, ``model``), so it
unit-tests without a gateway; the gateway supplies the real sandbox
verify, the producer's sampled value, and the citizen landing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..skills.contract import Slot

__all__ = [
    "ADAPTER_REPAIR_PROMPT",
    "ADAPTER_SYSTEM_PROMPT",
    "MAX_ADAPTER_REPAIRS",
    "AdapterResult",
    "AdapterSpec",
    "AdapterSynthesizer",
    "render_mapping_adapter",
]

MAX_ADAPTER_REPAIRS = 2

ADAPTER_SYSTEM_PROMPT = """\
You write ONE tiny adapter script that CONVERTS one node's output into
the shape the next node's input needs. The junction is a SHAPE change:
the value arrives under one name and type and must leave under the
consumer's name and type.

Rules:
- Reply with the COMPLETE script in one ```python fence.
- Read the incoming value from ./bindings.json (a JSON object). The
  produced value is under its PRODUCED name.
- CONVERT it — parse, reshape, re-type — to the consumer's declared
  type. Compute the real conversion from the real value; never fabricate,
  never bake in a constant, never name mock/placeholder/sample data.
- Emit exactly once, under the CONSUMER's name:
      from _oolu_runtime import emit_result
      emit_result({"<consumer name>": <converted value>})
- If the incoming value cannot be converted (it is missing or malformed),
  say so honestly instead of guessing:
      from _oolu_runtime import emit_error
      emit_error("...", kind="BadInput")
"""

ADAPTER_REPAIR_PROMPT = """\
You repair ONE adapter script. You will see the adapter and the exact
problem the gate named (a crash, or the consumer's port still not
satisfied). Edit, don't rewrite: keep the working parts. Reply with the
corrected COMPLETE script in one ```python fence. Same contract: read
./bindings.json, convert the produced value to the consumer's type, and
emit_result exactly once under the consumer's name.
"""


def render_mapping_adapter(produced_name: str, consumed_name: str) -> str:
    """The rename adapter — a DETERMINISTIC template, byte-stable for a
    given ``(produced_name, consumed_name)`` pair, never model-written.

    It reads the value the runtime bound under the producer's name and
    re-emits it verbatim under the consumer's name. Names are embedded as
    JSON string literals, so an odd name can never break the template or
    smuggle code in — the bytes are a pure function of the two names.
    """
    produced = json.dumps(str(produced_name))
    consumed = json.dumps(str(consumed_name))
    return (
        '"""A rename adapter — poured by the Paver, never model-written."""\n'
        "import json\n"
        "\n"
        "from _oolu_runtime import emit_error, emit_result\n"
        "\n"
        'with open("bindings.json", encoding="utf-8") as _fh:\n'
        "    _bindings = json.load(_fh)\n"
        "\n"
        f"if {produced} not in _bindings:\n"
        f'    emit_error("rename adapter received no " + {produced} + " value", '
        'kind="MissingInput")\n'
        "else:\n"
        f"    emit_result({{{consumed}: _bindings[{produced}]}})\n"
    )


@dataclass(frozen=True)
class AdapterSpec:
    """One junction to bridge: the producer's output port, the consumer's
    input port, the negotiated verdict, and the producer's REAL last-filed
    value as the test vector (the adapter is verified against real data,
    not an invented sample)."""

    produced: Slot
    consumed: Slot
    verdict: str  # "mappable" | "convertible"
    sample: Any = None


@dataclass(frozen=True)
class AdapterResult:
    """What one authoring attempt produced — or the exact reason it refused.

    ``ok`` means a screened, executed, port-checked adapter. ``templated``
    is True when it was poured deterministically (no model). ``rounds``
    counts the repair rounds a shape adapter used."""

    ok: bool
    verdict: str = ""
    script: str = ""
    templated: bool = False
    rounds: int = 0
    healed: tuple[str, ...] = field(default_factory=tuple)
    problem: str = ""


class AdapterSynthesizer:
    """Pour one adapter for one junction, verified by execution.

    ``verify`` is the birth-verify primitive (the F0
    ``verify_function(goal, script, *, session_id, ports=None,
    files=None)`` seam): it stages the sampled upstream value as
    ``bindings.json`` and checks the consumer's port on the result.
    ``model`` (``reply(messages) -> str``) authors SHAPE adapters only —
    a rename never touches it, so the deterministic path stays model-free.
    """

    def __init__(self, *, verify, model=None, max_repairs: int = MAX_ADAPTER_REPAIRS):
        self._verify = verify
        self._model = model
        self._max_repairs = max(0, max_repairs)

    # ------------------------------------------------------------------ #
    def author(self, spec: AdapterSpec) -> AdapterResult:
        """Bridge one junction. A ``mappable`` verdict pours the template;
        a ``convertible`` verdict authors with the model; anything else
        (a ``none`` the negotiator refused) builds nothing — advice
        proposes pairs, but only a passing test creates an edge."""
        if spec.verdict == "mappable":
            return self._pour_template(spec)
        if spec.verdict == "convertible":
            return self._author_shape(spec)
        return AdapterResult(
            ok=False,
            verdict=spec.verdict,
            problem=f"no adapter for a {spec.verdict!r} junction",
        )

    # ------------------------------------------------------------------ #
    def _pour_template(self, spec: AdapterSpec) -> AdapterResult:
        script = render_mapping_adapter(spec.produced.name, spec.consumed.name)
        screen = self._screen(script)
        if screen is not None:
            return AdapterResult(
                ok=False, verdict="mappable", script=script,
                templated=True, problem=screen,
            )
        report = self._verify_adapter(spec, script)
        if report.get("ok"):
            return AdapterResult(
                ok=True, verdict="mappable", script=script, templated=True,
                healed=tuple(report.get("healed") or ()),
            )
        # A template is deterministic; a real value it cannot pass is a
        # real signal (a misread role, a value the consumer's port
        # rejects) — no repair, refuse honestly.
        return AdapterResult(
            ok=False, verdict="mappable", script=script, templated=True,
            problem=report.get("error") or "the rename adapter failed verify",
        )

    def _author_shape(self, spec: AdapterSpec) -> AdapterResult:
        if self._model is None:
            return AdapterResult(
                ok=False, verdict="convertible",
                problem="a shape adapter needs a seated model and none is set",
            )
        script = self._first_draft(spec)
        if not script:
            return AdapterResult(
                ok=False, verdict="convertible",
                problem="the model wrote no usable adapter",
            )
        problem = ""
        rounds = 0
        for round_index in range(self._max_repairs + 1):
            rounds = round_index
            screen = self._screen(script)
            if screen is not None:
                problem = screen
            else:
                report = self._verify_adapter(spec, script)
                if report.get("ok"):
                    return AdapterResult(
                        ok=True, verdict="convertible", script=script,
                        rounds=round_index,
                        healed=tuple(report.get("healed") or ()),
                    )
                problem = report.get("error") or "the adapter failed verify"
            if round_index == self._max_repairs:
                break
            repaired = self._repair(spec, script, problem)
            if not repaired or repaired.strip() == script.strip():
                break
            script = repaired
        return AdapterResult(
            ok=False, verdict="convertible", script=script,
            rounds=rounds, problem=problem,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _screen(script: str) -> str | None:
        """The free gates before the sandbox — the safety screen and the
        mock screen, the same two every authored function passes."""
        from ..nodeplace.screening import mock_smells, screen_script

        flags = screen_script(script)
        if flags:
            return "refused by the safety screen: " + "; ".join(flags)
        smells = mock_smells(script)
        if smells:
            return "smells mocked: " + "; ".join(smells)
        return None

    def _verify_adapter(self, spec: AdapterSpec, script: str) -> dict:
        """Verify the adapter by EXECUTION: stage the producer's real
        sampled value as ``bindings.json`` and check the consumer's port
        on the emitted result — the one test that turns a candidate into
        an edge."""
        files = {
            "bindings.json": json.dumps(
                {spec.produced.name: spec.sample},
                ensure_ascii=False, sort_keys=True, default=str,
            )
        }
        ports = [{"name": spec.consumed.name, "type": spec.consumed.value_type}]
        return self._verify(
            f"adapt {spec.produced.name} -> {spec.consumed.name}",
            script,
            session_id=f"paver-adapt:{spec.produced.name}->{spec.consumed.name}",
            ports=ports,
            files=files,
        )

    def _first_draft(self, spec: AdapterSpec) -> str | None:
        content = (
            f"Produced value: name={spec.produced.name!r}, "
            f"type={spec.produced.value_type!r}"
            + (f", role={spec.produced.role!r}" if spec.produced.role else "")
            + "\n"
            f"Consumer needs: name={spec.consumed.name!r}, "
            f"type={spec.consumed.value_type!r}"
            + (f", role={spec.consumed.role!r}" if spec.consumed.role else "")
            + "\n\n"
            "A real sample of the produced value (this exact value will be "
            "staged as ./bindings.json when your adapter runs):\n"
            f"{json.dumps({spec.produced.name: spec.sample}, default=str)[:1000]}\n"
        )
        return self._consult(ADAPTER_SYSTEM_PROMPT, content)

    def _repair(self, spec: AdapterSpec, script: str, problem: str) -> str | None:
        content = (
            f"Adapter:\n```python\n{script}```\n\n"
            f"The gate refused with:\n{problem}\n\n"
            f"Convert {spec.produced.name!r} ({spec.produced.value_type}) to "
            f"{spec.consumed.name!r} ({spec.consumed.value_type})."
        )
        return self._consult(ADAPTER_REPAIR_PROMPT, content)

    def _consult(self, system: str, content: str) -> str | None:
        try:
            raw = self._model.reply(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ]
            )
        except Exception:  # noqa: BLE001 - a dead model authors nothing
            return None
        return _first_python_fence(raw)


_FENCE_START = "```"


def _first_python_fence(raw: str | None) -> str | None:
    """The first fenced code block of a reply, language tag stripped —
    the one script the adapter author is asked for."""
    text = raw or ""
    start = text.find(_FENCE_START)
    if start < 0:
        return None
    newline = text.find("\n", start)
    if newline < 0:
        return None
    end = text.find(_FENCE_START, newline + 1)
    if end < 0:
        return None
    body = text[newline + 1 : end].strip()
    return (body + "\n") if body else None
