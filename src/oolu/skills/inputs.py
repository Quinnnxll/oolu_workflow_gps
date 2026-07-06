"""Input manifests and value binding — creative values enter through holes.

The mechanical-design shape of a workflow: the scaffolding (open the app,
open the file, select the tool) is deterministic parameterized actions;
the *creative* step is values — dimensions, positions, text. A contract
declares those as ``ValueInput``s and its actions reference them with two
placeholder forms inside ``ActionEvent.parameters``:

- ``{"$input": "hole_radius"}`` — the whole parameter value is replaced
  by the resolved input (any declared type);
- ``{"$template": "cube([{width}, {depth}, {thickness}]);"}`` — a string
  with named holes. Only **number and choice** inputs may enter a
  template: free strings interpolated into source text are an injection
  vector, so they are refused at bind time, not discovered in production.

Resolution precedence is fixed: **user-provided > patcher-filled >
declared default**, every value is validated against its declaration
(numbers clamp into bounds, choices fall back to the default when the
filler hallucinates an option), and a required input with neither a
value nor a default refuses to bind — loudly, before anything runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .contract import (
    ActionsBody,
    NodeContract,
    SubgraphBody,
    ValueInput,
)

INPUT_KEY = "$input"
TEMPLATE_KEY = "$template"

_HOLE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class BoundInput:
    """One declared input, qualified by the node that owns it."""

    node_id: str
    node_name: str
    qualified: str  # "<node name>.<input name>" for subgraph children
    spec: ValueInput


def inputs_manifest(contract: NodeContract) -> list[BoundInput]:
    """Every declared input of the contract, with qualified names.

    A top-level (non-subgraph) contract uses bare input names; a
    subgraph qualifies each child's inputs as ``"<child name>.<name>"``.
    Two children sharing a name would make values unattributable, so
    that is refused here rather than mis-bound later.
    """
    body = contract.body
    if not isinstance(body, SubgraphBody):
        return [
            BoundInput(contract.id, contract.name, spec.name, spec)
            for spec in contract.inputs
        ]
    seen: dict[str, str] = {}
    manifest: list[BoundInput] = []
    for child in body.nodes:
        if child.inputs and child.name in seen and seen[child.name] != child.id:
            raise ValueError(
                f"two children named {child.name!r} both declare inputs; "
                "values would be unattributable"
            )
        seen[child.name] = child.id
        manifest.extend(
            BoundInput(child.id, child.name, f"{child.name}.{spec.name}", spec)
            for spec in child.inputs
        )
    return manifest


def validate_value(spec: ValueInput, value: Any) -> float | str:
    """Coerce and box one offered value. The declaration always wins:
    numbers clamp into [minimum, maximum]; a choice outside the set falls
    back to the default; strings are just strings."""
    if spec.value_type == "number":
        number = float(value)  # raises for garbage: the caller decides
        if spec.minimum is not None:
            number = max(number, spec.minimum)
        if spec.maximum is not None:
            number = min(number, spec.maximum)
        return number
    if spec.value_type == "choice":
        text = str(value)
        if spec.choices and text not in spec.choices:
            if spec.default is None:
                raise ValueError(
                    f"{spec.name!r}: {text!r} is not one of {spec.choices}"
                )
            return spec.default
        return text
    return str(value)


def resolve_values(
    manifest: Sequence[BoundInput],
    provided: Mapping[str, Any] | None = None,
    *,
    strict: bool = True,
) -> dict[str, float | str]:
    """Final value per qualified name: provided > default, validated.

    ``strict`` refuses unknown provided keys (an API caller misspelling
    an input deserves a 400, not silence). A malformed provided value
    (garbage where a number belongs) falls back to the default when one
    exists — a bad filler degrades to the honest default, it never
    blocks — and errors only when there is nothing to fall back to.
    """
    by_name = {entry.qualified: entry.spec for entry in manifest}
    provided = dict(provided or {})
    if strict:
        unknown = sorted(set(provided) - set(by_name))
        if unknown:
            raise ValueError(f"unknown inputs: {', '.join(unknown)}")

    resolved: dict[str, float | str] = {}
    for qualified, spec in by_name.items():
        if qualified in provided:
            try:
                resolved[qualified] = validate_value(spec, provided[qualified])
                continue
            except (TypeError, ValueError):
                pass  # a garbage offer degrades to the default below
        if spec.default is not None:
            resolved[qualified] = validate_value(spec, spec.default)
        elif spec.required:
            raise ValueError(f"input {qualified!r} is required and has no default")
    return resolved


def bind_inputs(
    contract: NodeContract, values: Mapping[str, Any] | None = None
) -> NodeContract:
    """Substitute resolved values into every placeholder; return the
    concrete contract the compiler and executors see.

    Values that never got a placeholder are fine (declared for future
    bodies); a placeholder naming an undeclared input is an authoring
    bug and refuses to bind. Binding a contract with no placeholders is
    the identity — safe to call unconditionally.
    """
    manifest = inputs_manifest(contract)
    resolved = resolve_values(manifest, values)

    def bind_node(node: NodeContract, prefix: str) -> NodeContract:
        local = {
            entry.spec.name: resolved[entry.qualified]
            for entry in manifest
            if entry.node_id == node.id
        }
        types = {spec.name: spec.value_type for spec in node.inputs}
        body = node.body
        if not isinstance(body, ActionsBody):
            return node
        actions = [
            action.model_copy(
                update={
                    "parameters": _substitute(
                        action.parameters, local, types, node.name
                    )
                }
            )
            for action in body.actions
        ]
        return node.model_copy(update={"body": ActionsBody(actions=actions)})

    body = contract.body
    if isinstance(body, SubgraphBody):
        children = [bind_node(child, child.name) for child in body.nodes]
        return contract.model_copy(
            update={"body": SubgraphBody(nodes=children, edges=list(body.edges))}
        )
    return bind_node(contract, contract.name)


def _substitute(value: Any, local: dict, types: dict, node_name: str) -> Any:
    if isinstance(value, dict):
        if set(value) == {INPUT_KEY}:
            name = value[INPUT_KEY]
            if name not in local:
                raise ValueError(f"{node_name!r} references undeclared input {name!r}")
            return local[name]
        if set(value) == {TEMPLATE_KEY}:
            return _render_template(value[TEMPLATE_KEY], local, types, node_name)
        return {
            key: _substitute(item, local, types, node_name)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_substitute(item, local, types, node_name) for item in value]
    return value


def _render_template(template: str, local: dict, types: dict, node_name: str) -> str:
    """Fill a template's named holes — numbers and choices only.

    A free string interpolated into source text could smuggle syntax
    (the `"); do_evil(` classic); refusing it here makes the whole class
    of injection unrepresentable rather than merely discouraged.
    """
    rendered = template
    for hole in set(_HOLE_RE.findall(template)):
        if hole not in local:
            raise ValueError(
                f"{node_name!r}: template hole {{{hole}}} names an undeclared input"
            )
        if types.get(hole) == "string":
            raise ValueError(
                f"{node_name!r}: free-string input {hole!r} may not enter a "
                'template (injection risk); pass it whole via {"$input": ...}'
            )
        value = local[hole]
        text = format(value, "g") if isinstance(value, float) else str(value)
        rendered = rendered.replace("{" + hole + "}", text)
    return rendered
