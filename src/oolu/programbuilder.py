"""The program author (F1): plan → module loop → deterministic dispatcher.

The builder writes trees, one verified module at a time:

1. ``plan_program`` — ONE consultation returns the whole
   :class:`ProgramSpec` (modules with purposes, APIs, dependencies and
   check paths; operations; the one interface) plus the declared inputs.
   A plan that breaks the spec's laws refuses with the parser's exact
   words — ceilings, cycles, reserved ports, the lot.
2. ``author_module`` — one consultation per module, in dependency-
   topological order, each gated the moment it arrives: the safety
   screen, the mock screen (modules must compute; their checks may emit
   a status), then the module's own check run in the sandbox WITH the
   partial tree staged. A failing gate buys bounded repair rounds
   (edit-don't-rewrite, the birth-repair discipline) BEFORE the next
   module is authored — fail fast per module, never whole-program
   repair at the end.
3. ``render_dispatcher`` — the program's one face is a DETERMINISTIC
   template, never model-written: it selects the operation, imports the
   entry, calls it with the bindings dict, and speaks ``emit_result``
   exactly once. A zero-input program reads NO ``bindings.json`` at all
   — the static birth wall refuses a bindings-reading script with no
   declared inputs, and the wall is right.

The cost bound is honest: ``1 + M×(1 author + ≤2 repairs)``
consultations, every one counted on the build result; each check run
may cost up to 3 backend executions for dependency healing, and the
receipt says so.

Vocabulary: always "program node"; the entry-function contract every
module author writes against is ONE positional dict of inputs in, the
result payload dict out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .skills.program import (
    ProgramSpec,
    canonical_program_json,
    parse_program_spec,
)

__all__ = [
    "MODULE_SYSTEM_PROMPT",
    "PROGRAM_PLAN_PROMPT",
    "ProgramAuthor",
    "ProgramBuild",
    "plan_program",
    "render_dispatcher",
]

MAX_MODULE_REPAIRS = 2

PROGRAM_PLAN_PROMPT = """\
You plan PROGRAM NODES: one node holding a small internal program —
several modules behind ONE deterministic entry — larger than a single
script, smaller than an app.

Reply with exactly ONE JSON object (in a ```json fence) of this shape:

{
  "modules": [
    {"path": "lib/ingest.py", "purpose": "…", "depends": [],
     "check": "tests/check_ingest.py",
     "api": [{"name": "load", "takes": ["source"], "returns": "rows"}]}
  ],
  "operations": [
    {"name": "main", "entry": "lib.report:build"}
  ],
  "interface": {
    "operation": "main",
    "ports": [{"name": "summary", "type": "str"}]
  },
  "inputs": [{"name": "source_folder", "type": "str",
              "label": "Which folder holds the files?"}]
}

The laws:
- 2 to 12 modules under lib/, each ONE responsibility, each with a
  check script under tests/ that imports it and asserts REAL behavior.
- "depends" lists sibling module paths a module imports — no cycles.
- ONE interface: one externally-invoked operation, its result ports.
  Never name a port "state", "files", or "records".
- Every operation's "entry" is "dotted.module:function". That function
  takes ONE dict of inputs and RETURNS the result payload dict carrying
  the interface ports.
- "inputs" are what the user must provide, with plain-word labels —
  never technical mechanism questions.
- If the request is conversation, not a buildable program, reply with
  the single word NO_TASK.
"""

MODULE_SYSTEM_PROMPT = """\
You write ONE internal module of a program node.

Rules:
- Reply with the module's COMPLETE code in one ```python fence. Plain
  importable Python: top-level functions, no side effects at import, no
  emit_result — only the dispatcher and check scripts speak the runtime
  contract.
- Compute real things from real inputs. Never fabricate data, never
  bake in a constant answer, never name mock/placeholder/sample data —
  a module that cannot reach its data raises a clear exception instead.
- An operation entry function (named in the spec) takes ONE dict of
  inputs and RETURNS the result payload dict carrying the interface
  ports.
- If the module declares a check, ALSO reply with the check script in a
  SECOND ```python fence: it imports the module, asserts real behavior,
  and ends with:
      from _oolu_runtime import emit_result
      emit_result({"checked": "<module path>"})
"""

MODULE_REPAIR_PROMPT = """\
You repair ONE internal module of a program node. You will see the
module, its check, and the exact problem the gate named. Edit, don't
rewrite: keep the working parts. Reply with the corrected COMPLETE
module in one ```python fence; if the check itself was wrong, also
reply with the corrected check in a SECOND ```python fence.
"""

_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(\{.*?\})\s*```", re.DOTALL)


def _python_fences(raw: str) -> list[str]:
    return [m.group(1).strip() + "\n" for m in _FENCE_RE.finditer(raw or "")]


def _extract_json(raw: str) -> dict | None:
    """The plan object from a reply — fenced first, else the first
    balanced top-level object. None when nothing parses."""
    text = raw or ""
    match = _JSON_FENCE_RE.search(text)
    candidates = [match.group(1)] if match else []
    start = text.find("{")
    if start >= 0:
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : index + 1])
                    break
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def plan_program(
    model, goal: str
) -> tuple[ProgramSpec | None, dict, str]:
    """``(spec, io, problem)`` — one consultation, the parser's refusals
    verbatim. ``io`` is ``{"inputs": [...], "outputs": [...]}``, outputs
    mirroring the interface ports."""
    try:
        raw = model.reply(
            [
                {"role": "system", "content": PROGRAM_PLAN_PROMPT},
                {"role": "user", "content": f"Plan a program node for:\n{goal}"},
            ]
        )
    except Exception as exc:  # noqa: BLE001 - a dead model plans nothing
        return None, {}, f"the model could not be reached to plan: {exc}"
    if "NO_TASK" in (raw or "").strip().upper()[:40]:
        return None, {}, (
            "that reads as conversation, not a buildable program"
        )
    declared = _extract_json(raw)
    if declared is None:
        return None, {}, "the model wrote no usable program plan"
    inputs = [
        {
            "name": str(item.get("name", "")).strip(),
            "type": str(item.get("type", "str")).strip() or "str",
            "label": str(item.get("label", "")).strip(),
        }
        for item in (declared.pop("inputs", None) or [])
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    ]
    spec, problem = parse_program_spec(declared)
    if problem:
        return None, {}, problem
    if len(spec.modules) < 2:
        return None, {}, (
            "a program node holds at least two internal modules — for a "
            "single function, ask for a node instead"
        )
    if not spec.operations:
        return None, {}, "a program declares at least one operation"
    outputs = [
        {
            "name": str(p.get("name", "")).strip(),
            "type": str(p.get("type", "str")).strip() or "str",
            **(
                {"label": str(p.get("label", "")).strip()}
                if str(p.get("label", "")).strip()
                else {}
            ),
        }
        for p in spec.interface.ports
    ]
    return spec, {"inputs": inputs, "outputs": outputs}, ""


def render_dispatcher(spec: ProgramSpec, io: dict | None = None) -> str:
    """The program's one face — a deterministic template, never
    model-written, so the unified interface cannot be mocked. Zero
    declared inputs means ZERO ``bindings.json`` read (the static birth
    wall's rule); operations dispatch by the ``_operation`` binding,
    defaulting to the interface's own."""
    inputs = list((io or {}).get("inputs") or [])
    entries = {op.name: op.entry for op in spec.operations}
    default = (
        spec.interface.operation
        if spec.interface.operation in entries
        else sorted(entries)[0]
    )
    lines = [
        '"""The program\'s one face — generated by the builder, never '
        'model-written."""',
        "import importlib",
        "",
        "from _oolu_runtime import emit_result",
        "",
    ]
    if inputs:
        lines += [
            "import json",
            "",
            'with open("bindings.json", encoding="utf-8") as _fh:',
            "    BINDINGS = json.load(_fh)",
        ]
    else:
        lines += ["BINDINGS = {}"]
    lines += [
        "",
        "_OPERATIONS = {",
        *[f'    "{name}": "{entries[name]}",' for name in sorted(entries)],
        "}",
        f'_DEFAULT = "{default}"',
        '_name = str(BINDINGS.get("_operation") or _DEFAULT)',
        "if _name not in _OPERATIONS:",
        '    raise SystemExit("unknown operation: " + _name)',
        '_module_name, _, _func_name = _OPERATIONS[_name].partition(":")',
        "_module = importlib.import_module(_module_name)",
        "emit_result(getattr(_module, _func_name)(dict(BINDINGS)))",
    ]
    return "\n".join(lines) + "\n"


def _package_inits(paths: list[str]) -> dict[str, str]:
    """Deterministic scaffolding: an empty ``__init__.py`` for every
    package directory a module path implies — never model-written."""
    inits: dict[str, str] = {}
    for path in paths:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            inits[f"{'/'.join(parts[:depth])}/__init__.py"] = ""
    return inits


def _def_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in (text or "").splitlines()
        if line.strip().startswith("def ")
    ]


@dataclass
class ProgramBuild:
    """What one build attempt produced — or the exact reason it refused."""

    ok: bool
    problem: str = ""
    spec: ProgramSpec | None = None
    script: str = ""
    files: dict[str, str] = field(default_factory=dict)
    io: dict = field(default_factory=dict)
    states: list[str] = field(default_factory=list)
    consultations: int = 0
    notes: list[str] = field(default_factory=list)


class ProgramAuthor:
    """The staged pipeline. ``model`` speaks ``reply(messages) -> str``
    (the one-shot author's interface, so every seated model works);
    ``verify`` is the birth-verify primitive
    (``verify_function(goal, script, *, session_id, ports=None,
    files=None)``) — the F0 seam, judged with the partial tree staged."""

    def __init__(self, model, *, verify, max_repairs: int = MAX_MODULE_REPAIRS):
        self._model = model
        self._verify = verify
        self._max_repairs = max(0, max_repairs)

    # ------------------------------------------------------------------ #
    def build(self, goal: str) -> ProgramBuild:
        spec, io, problem = plan_program(self._model, goal)
        consultations = 1
        if problem:
            return ProgramBuild(
                ok=False,
                problem=problem,
                states=["plan-refused"],
                consultations=consultations,
            )
        states = [f"planned:{len(spec.modules)}-modules"]
        notes: list[str] = []
        files = _package_inits([m.path for m in spec.modules])

        for module in self._topological(spec):
            text, check, author_problem = self._author_module(
                goal, spec, module, files
            )
            consultations += 1
            if author_problem:
                return ProgramBuild(
                    ok=False,
                    problem=f"module '{module.path}': {author_problem}",
                    spec=spec,
                    states=[*states, f"module:{module.path}:author-refused"],
                    consultations=consultations,
                )
            for round_index in range(self._max_repairs + 1):
                gate_problem, note = self._module_problem(
                    module, text, check, files
                )
                if note:
                    notes.append(note)
                if gate_problem is None:
                    break
                if round_index == self._max_repairs:
                    return ProgramBuild(
                        ok=False,
                        problem=(
                            f"module '{module.path}' failed after "
                            f"{self._max_repairs} repair rounds: "
                            f"{gate_problem}"
                        ),
                        spec=spec,
                        states=[*states, f"module:{module.path}:exhausted"],
                        consultations=consultations,
                    )
                text, check, repair_problem = self._repair_module(
                    module, text, check, gate_problem
                )
                consultations += 1
                if repair_problem:
                    return ProgramBuild(
                        ok=False,
                        problem=(
                            f"module '{module.path}' could not be repaired: "
                            f"{repair_problem}"
                        ),
                        spec=spec,
                        states=[*states, f"module:{module.path}:repair-refused"],
                        consultations=consultations,
                    )
            files[module.path] = text
            if module.check is not None and check is not None:
                files[module.check] = check
            states.append(f"module:{module.path}:verified")

        script = render_dispatcher(spec, io)
        return ProgramBuild(
            ok=True,
            spec=spec,
            script=script,
            files=files,
            io=io,
            states=[*states, "dispatcher-rendered"],
            consultations=consultations,
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _topological(spec: ProgramSpec):
        """Author order: dependencies first (the parser proved acyclic);
        declaration order breaks ties, so builds are reproducible."""
        remaining = {m.path: m for m in spec.modules}
        done: set[str] = set()
        order = []
        while remaining:
            ready = [
                path
                for path, module in remaining.items()
                if set(module.depends) <= done
            ]
            if not ready:
                # The parser rejects every cycle, so this is unreachable
                # through ``build`` — but a direct caller on an unvalidated
                # spec must not spin. Append the remainder in a stable order
                # rather than hang; the per-module gate will surface any
                # broken import.
                order.extend(remaining[path] for path in sorted(remaining))
                break
            for path in ready:
                order.append(remaining.pop(path))
                done.add(path)
        return order

    def _author_module(self, goal, spec, module, files):
        written = "\n".join(
            f"- {path}: " + "; ".join(_def_lines(text)) if _def_lines(text) else f"- {path}"
            for path, text in sorted(files.items())
            if path.endswith(".py") and files[path]
        )
        api = "; ".join(
            f"{sig.name}({', '.join(sig.takes)}) -> {sig.returns}"
            for sig in module.api
        )
        content = (
            f"Program goal:\n{goal}\n\n"
            f"The plan (program.json):\n{canonical_program_json(spec)}\n\n"
            f"Write the module: {module.path}\n"
            f"Purpose: {module.purpose or '(as the plan implies)'}\n"
            + (f"Declared API: {api}\n" if api else "")
            + (f"Its check script: {module.check}\n" if module.check else "")
            + (f"\nAlready written:\n{written}\n" if written else "")
        )
        try:
            raw = self._model.reply(
                [
                    {"role": "system", "content": MODULE_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ]
            )
        except Exception as exc:  # noqa: BLE001 - a dead model writes nothing
            return None, None, f"the model could not be reached: {exc}"
        fences = _python_fences(raw)
        if not fences:
            return None, None, "the model wrote no usable module code"
        text = fences[0]
        check = fences[1] if len(fences) > 1 else None
        if module.check is not None and check is None:
            return None, None, (
                f"the plan declares a check ({module.check}) but the model "
                "wrote none"
            )
        return text, check, ""

    def _repair_module(self, module, text, check, problem):
        content = (
            f"Module: {module.path}\n```python\n{text}```\n\n"
            + (
                f"Its check ({module.check}):\n```python\n{check}```\n\n"
                if check is not None
                else ""
            )
            + f"The gate refused with:\n{problem}\n"
        )
        try:
            raw = self._model.reply(
                [
                    {"role": "system", "content": MODULE_REPAIR_PROMPT},
                    {"role": "user", "content": content},
                ]
            )
        except Exception as exc:  # noqa: BLE001
            return None, None, f"the model could not be reached: {exc}"
        fences = _python_fences(raw)
        if not fences:
            return None, None, "the repair reply carried no code"
        repaired = fences[0]
        repaired_check = fences[1] if len(fences) > 1 else check
        return repaired, repaired_check, ""

    def _module_problem(self, module, text, check, files):
        """``(problem, note)`` — the per-module gates, text first (free),
        sandbox last (the check with the partial tree staged). A check's
        honest emit_error passes with a note: at authoring time, with no
        real bindings, an honest refusal names missing data."""
        from .nodeplace.screening import mock_smells, screen_script

        flags = screen_script(text)
        if flags:
            return "refused by the safety screen: " + "; ".join(flags), ""
        smells = mock_smells(text)
        if smells:
            return "smells mocked: " + "; ".join(smells), ""
        if check is not None:
            check_flags = screen_script(check)
            if check_flags:
                return (
                    "its check is refused by the safety screen: "
                    + "; ".join(check_flags),
                    "",
                )
        if module.check is None or check is None:
            return None, ""
        staged = {**files, module.path: text, module.check: check}
        report = self._verify(
            f"check module {module.path}",
            check,
            session_id=f"program-author:{module.path}",
            files=staged,
        )
        if report.get("ok"):
            return None, ""
        if report.get("honest_error"):
            return None, (
                f"check {module.check}: honest error — "
                + str(report.get("error", ""))[:200]
            )
        return str(report.get("error") or "the check failed"), ""
