"""The program node's vocabulary — a tree with one face (F0).

A "multi-module backend service" in OoLu is NOT a daemon (Phase B forbids
residency, and that stands). It is a **program node**: a drawer tree of
internal modules with declared module-level interfaces, a set of internal
operations invoked per run through one deterministic dispatcher, named
program state, and exactly ONE unified interface — the node's declared
ports plus one view — presenting the program's standing result. The
external contract (Slots/ports) stays singular; internal ``OpSig``s never
touch routing or ``derive_data_edges`` — a program's insides are
implementation, its citizen face is one contract.

The spec is DATA the node ships: ``src/program.json`` in the drawer tree,
canonically serialized (sorted keys, no whitespace variance) so the same
spec always freezes to the same ``bundle_id`` — an edit to the spec is an
edit to the tree, and the runner re-verifies it for free through
bundle-in-cache-key.

``parse_program_spec`` refuses loudly, by name, at the door: more than one
interface (structural — the field is singular), more modules than the
ceiling, dependency cycles, paths that escape the tree, mechanism-flavored
port labels (the B0 lexicon), and any port named with a RESERVED payload
key — the keys completion hooks consume as side channels; a port so named
would have its value silently eaten as a command.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_PROGRAM_MODULES",
    "RESERVED_PAYLOAD_KEYS",
    "ModuleSpec",
    "OpSig",
    "OperationSpec",
    "ProgramSpec",
    "StateSpec",
    "UnifiedInterface",
    "ViewSpec",
    "canonical_program_json",
    "parse_program_spec",
]

# The keys completion hooks claim out of an emitted payload as side
# channels: "records" lands the node's book, "files" lands emitted
# documents, "state" is F2's program state. A declared output PORT with
# one of these names would have its value consumed as a command instead
# of filed as an answer — refused at declaration, on every path.
RESERVED_PAYLOAD_KEYS: tuple[str, ...] = ("files", "records", "state")

MAX_PROGRAM_MODULES = 12


class OpSig(BaseModel):
    """One internal operation of one module — NOT a Slot, invisible to
    routing. What the builder plans against and the checks judge."""

    model_config = ConfigDict(frozen=True)

    name: str
    takes: list[str] = Field(default_factory=list)
    returns: str = ""
    description: str = ""


class ModuleSpec(BaseModel):
    """One internal module: its place in the tree, its purpose, its
    interface, its dependencies, and its birth check."""

    model_config = ConfigDict(frozen=True)

    path: str  # "lib/ingest.py" — POSIX-relative, bundle-safe
    purpose: str = ""
    api: list[OpSig] = Field(default_factory=list)
    # Sibling module paths this module imports. The authoring order is
    # the topological order over these; a cycle refuses at parse.
    depends: list[str] = Field(default_factory=list)
    check: str | None = None  # "tests/check_ingest.py" — per-module birth check


class OperationSpec(BaseModel):
    """One invocable operation: the dispatcher target plus the state it
    declares it touches (F2 stages exactly what an operation reads)."""

    model_config = ConfigDict(frozen=True)

    name: str
    entry: str  # "lib.report:build"
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)


class StateSpec(BaseModel):
    """One named program state: drawer ``state/<name>.json``, staged
    ``./state/<name>.json``. ``rows`` reuses the Life-books typed-row
    discipline; ``value`` is one JSON value replaced whole."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: Literal["rows", "value"] = "rows"
    schema_hint: str = ""


class ViewSpec(BaseModel):
    """How the node presents its standing result. ``ports`` (default,
    every node free) is the deterministic server-rendered view; ``html``
    is the named follow-on behind a scoped view credential — modeled now
    so specs round-trip, served later."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["ports", "html"] = "ports"
    entry: str = ""


class UnifiedInterface(BaseModel):
    """The ONE external face — singular BY CONSTRUCTION: one field, never
    a list. One externally-invoked operation, the declared ports, one
    view."""

    model_config = ConfigDict(frozen=True)

    operation: str = "main"
    ports: list[dict] = Field(default_factory=list)  # runtime/contract.py port dicts
    view: ViewSpec = Field(default_factory=ViewSpec)


class ProgramSpec(BaseModel):
    """The whole program, frozen. Canonical serialization keeps
    ``bundle_id`` stable: the same spec is the same bytes, always."""

    model_config = ConfigDict(frozen=True)

    modules: list[ModuleSpec] = Field(default_factory=list)
    operations: list[OperationSpec] = Field(default_factory=list)
    state: list[StateSpec] = Field(default_factory=list)
    interface: UnifiedInterface = Field(default_factory=UnifiedInterface)
    limits_profile: Literal["step", "program"] = "step"


def canonical_program_json(spec: ProgramSpec) -> str:
    """The one serialized form: sorted keys, no whitespace variance —
    identical specs freeze to identical bundle ids."""
    return json.dumps(
        spec.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _unsafe_path(path: str) -> bool:
    """Bundle-safe means POSIX-relative and inside the tree: no absolute
    paths, no drive letters, no ``..``, no backslashes."""
    if not path or path.startswith("/") or "\\" in path or ":" in path:
        return True
    return any(part in ("", "..") for part in path.split("/"))


def parse_program_spec(raw: Any) -> tuple[ProgramSpec | None, str]:
    """``(spec, "")`` or ``(None, problem)`` — the refusal style: a spec
    that cannot be honored refuses by name, never degrades silently."""
    if isinstance(raw, ProgramSpec):
        spec = raw
    else:
        if not isinstance(raw, dict):
            return None, (
                "a program spec must be a JSON object, got "
                f"{type(raw).__name__}"
            )
        if isinstance(raw.get("interface"), list):
            # The structural law, enforced against the raw shape too: a
            # model offering a LIST of interfaces is refused, not truncated.
            return None, (
                "a program node has ONE unified interface — 'interface' "
                "must be an object, not a list"
            )
        try:
            spec = ProgramSpec.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - the refusal carries the reason
            return None, f"the program spec does not parse: {exc}"

    if len(spec.modules) > MAX_PROGRAM_MODULES:
        return None, (
            f"a program holds at most {MAX_PROGRAM_MODULES} modules "
            f"({len(spec.modules)} declared) — split the node: one "
            "capability per node"
        )

    paths: list[str] = []
    for module in spec.modules:
        if _unsafe_path(module.path):
            return None, (
                f"module path '{module.path}' escapes the tree — paths are "
                "POSIX-relative, inside the drawer, no '..'"
            )
        if module.check is not None and _unsafe_path(module.check):
            return None, (
                f"check path '{module.check}' escapes the tree — paths are "
                "POSIX-relative, inside the drawer, no '..'"
            )
        paths.append(module.path)
    if len(set(paths)) != len(paths):
        dupes = sorted({p for p in paths if paths.count(p) > 1})
        return None, f"duplicate module path(s): {', '.join(dupes)}"

    known = set(paths)
    # A check may not BE a module source (F0.1): the birth door exempts
    # check paths from the mock screen — a module that named its own
    # source as its check would exempt genuinely mockable code from the
    # one screen that catches fabrication.
    module_paths = set(paths)
    for module in spec.modules:
        if module.check is not None and module.check in module_paths:
            return None, (
                f"module '{module.path}' names a module source "
                f"('{module.check}') as its check — a check must be a "
                "separate script that asserts against the module"
            )
    for module in spec.modules:
        for dep in module.depends:
            if dep not in known:
                return None, (
                    f"module '{module.path}' depends on '{dep}', which is "
                    "not a declared module"
                )
    # Topological check over depends — authoring order must exist.
    resolved: set[str] = set()
    remaining = {m.path: set(m.depends) for m in spec.modules}
    while remaining:
        ready = [p for p, deps in remaining.items() if deps <= resolved]
        if not ready:
            cycle = ", ".join(sorted(remaining))
            return None, (
                f"module dependencies form a cycle among: {cycle} — an "
                "authoring order must exist"
            )
        for p in ready:
            resolved.add(p)
            remaining.pop(p)

    operation_names = {op.name for op in spec.operations}
    if len(operation_names) != len(spec.operations):
        return None, "duplicate operation names"
    for op in spec.operations:
        module_part, _, func_part = op.entry.partition(":")
        if not module_part or not func_part or "/" in op.entry:
            return None, (
                f"operation '{op.name}' entry must be "
                f"'dotted.module:function', got '{op.entry}'"
            )
    if spec.operations and spec.interface.operation not in operation_names:
        return None, (
            f"the interface invokes operation '{spec.interface.operation}', "
            "which is not declared"
        )
    state_names = {s.name for s in spec.state}
    for op in spec.operations:
        for name in (*op.reads, *op.writes):
            if name not in state_names:
                return None, (
                    f"operation '{op.name}' touches state '{name}', which "
                    "is not declared"
                )

    from ..plainlanguage import mechanism_terms

    for port in spec.interface.ports:
        name = str(port.get("name", "")).strip()
        if not name:
            return None, "an interface port needs a name"
        if name in RESERVED_PAYLOAD_KEYS:
            return None, (
                f"the port '{name}' is a reserved payload key — completion "
                "hooks consume it as a side channel; name the result "
                "something else"
            )
        label = str(port.get("label", "")).strip()
        if label:
            tripped = mechanism_terms(label)
            if tripped:
                return None, (
                    f"the port '{name}' is labeled with a technical "
                    f"question ({', '.join(tripped)}) — labels name a value "
                    "in the user's world, in plain words"
                )
    return spec, ""
