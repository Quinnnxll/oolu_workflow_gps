"""Demonstration compilers: exact replay first, generalization from repetition.

``compile_exact`` (the conservative default) freezes a single demonstration —
byte-for-byte replay gated on the demonstrated workspace fingerprint.

``compile_generalized`` is the growth path: when the user has demonstrated the
*same* task two or more times, the aligned action streams are diffed —

- a parameter value that **varies** across demonstrations becomes a typed slot
  (a ``SkillParameter``), with its observed values kept as the domain;
- a value that stays **constant** becomes part of the skill body;
- identical varying values at different positions unify into one slot (the
  same file referenced twice stays one parameter);
- strings under the demonstrated workspace root are templated against
  ``{workspace}``, so learned skills never pin the user's absolute paths and
  replay in any workspace.

``bind_parameters`` turns a generalized skill plus concrete arguments back
into executable ``ActionEvent``s. Together these are how node construction
compounds over time: every repeated demonstration widens what the skill can
do without a model in the loop.
"""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any, Sequence

from .models import (
    ActionEvent,
    ConstraintSeverity,
    ConstraintSpec,
    ConstraintStatus,
    Demonstration,
    ExecutionStatus,
    ReusableSkill,
    SkillParameter,
    SkillSignature,
)
from .workspace import changed_artifacts

_PARAM_MARKER = "$param"
_WORKSPACE_TOKEN = "{workspace}"
_NAME_RE = re.compile(r"[^a-z0-9]+")


class DemonstrationCompiler:
    def compile_exact(
        self,
        demonstration: Demonstration,
        *,
        name: str,
        description: str,
        signature: SkillSignature,
    ) -> ReusableSkill:
        if demonstration.before is None or demonstration.after is None:
            raise ValueError("an exact skill requires before and after snapshots")
        artifacts = changed_artifacts(demonstration.before, demonstration.after)
        if not artifacts:
            raise ValueError(
                "an exact skill must produce at least one changed artifact"
            )
        precondition = ConstraintSpec(
            id="workspace-before-fingerprint",
            description="Workspace must match the demonstrated starting state",
            validator="workspace.before_fingerprint",
            severity=ConstraintSeverity.HARD,
            status=ConstraintStatus.SATISFIED,
            evidence={"expected_fingerprint": demonstration.before.fingerprint},
        )
        result_constraint = ConstraintSpec(
            id="workspace-expected-artifacts",
            description="Demonstrated output artifacts must be reproduced",
            validator="workspace.expected_artifacts",
            severity=ConstraintSeverity.HARD,
            status=ConstraintStatus.SATISFIED,
            evidence={"expected_files": artifacts},
        )
        return ReusableSkill(
            name=name,
            description=description,
            signature=signature,
            parameters=[],
            preconditions=[precondition],
            actions=demonstration.actions,
            validators=[result_constraint],
            demonstration_ids=[demonstration.id],
        )

    # ------------------------------------------------------------------ #
    # Generalization from repeated demonstrations.                        #
    # ------------------------------------------------------------------ #
    def compile_generalized(
        self,
        demonstrations: Sequence[Demonstration],
        *,
        name: str,
        description: str,
        signature: SkillSignature,
    ) -> ReusableSkill:
        demos = list(demonstrations)
        if len(demos) < 2:
            raise ValueError(
                "generalization requires at least two demonstrations of the task"
            )
        for demo in demos:
            if demo.outcome is not ExecutionStatus.SUCCEEDED:
                raise ValueError(
                    "only successful demonstrations may be generalized "
                    f"(demonstration {demo.id} is {demo.outcome.value})"
                )
        skeleton = [(a.adapter, a.operation) for a in demos[0].actions]
        if not skeleton:
            raise ValueError("demonstrations contain no actions")
        for demo in demos[1:]:
            if [(a.adapter, a.operation) for a in demo.actions] != skeleton:
                raise ValueError(
                    "demonstrations do not share one action skeleton "
                    "(same operations in the same order); refusing to guess"
                )

        roots = [_workspace_root(demo) for demo in demos]
        induction = _SlotInduction()
        templated_actions: list[ActionEvent] = []
        for index, (adapter, operation) in enumerate(skeleton):
            variants = [
                _template_workspace(demo.actions[index].parameters, root)
                for demo, root in zip(demos, roots)
            ]
            merged = induction.merge(variants, path=(operation,))
            reference = demos[0].actions[index]
            templated_actions.append(
                ActionEvent(
                    correlation_id=reference.correlation_id,
                    adapter=adapter,
                    operation=operation,
                    parameters=merged,
                    actor=reference.actor,
                    credential_ref=reference.credential_ref,
                )
            )

        validators: list[ConstraintSpec] = []
        common = _common_artifacts(demos)
        if common:
            validators.append(
                ConstraintSpec(
                    id="workspace-expected-artifacts",
                    description="Artifacts produced in every demonstration must be reproduced",
                    validator="workspace.expected_artifacts",
                    severity=ConstraintSeverity.HARD,
                    status=ConstraintStatus.SATISFIED,
                    evidence={"expected_files": common},
                )
            )

        return ReusableSkill(
            name=name,
            description=description,
            signature=signature,
            parameters=induction.parameters(),
            preconditions=[],  # generalization relaxes the fingerprint pin
            actions=templated_actions,
            validators=validators,
            demonstration_ids=[demo.id for demo in demos],
        )


# --------------------------------------------------------------------------- #
# Slot induction: diff aligned parameter trees.                               #
# --------------------------------------------------------------------------- #
class _SlotInduction:
    """Collects slots while merging aligned parameter trees.

    Identical observation tuples share one slot: if the demos always used the
    same value at two positions, both positions bind to a single parameter.
    """

    def __init__(self):
        self._by_observations: dict[tuple, str] = {}
        self._params: dict[str, SkillParameter] = {}

    def merge(self, variants: list[Any], *, path: tuple[str, ...]) -> Any:
        first = variants[0]
        if all(v == first for v in variants[1:]):
            return first
        if all(isinstance(v, dict) for v in variants) and all(
            set(v) == set(first) for v in variants[1:]
        ):
            return {
                key: self.merge([v[key] for v in variants], path=path + (str(key),))
                for key in first
            }
        if all(isinstance(v, list) for v in variants) and all(
            len(v) == len(first) for v in variants[1:]
        ):
            return [
                self.merge([v[i] for v in variants], path=path + (str(i),))
                for i in range(len(first))
            ]
        return {_PARAM_MARKER: self._slot(variants, path)}

    def _slot(self, observed: list[Any], path: tuple[str, ...]) -> str:
        key = tuple(_freeze(v) for v in observed)
        existing = self._by_observations.get(key)
        if existing is not None:
            return existing
        name = self._unique_name(path)
        self._by_observations[key] = name
        value_type, role = _infer_type(observed)
        domain: dict[str, Any] = {"observed": list(observed)}
        if role is not None:
            domain["role"] = role
        self._params[name] = SkillParameter(
            name=name,
            value_type=value_type,
            required=True,
            description=f"varies across demonstrations at {'.'.join(path)}",
            domain=domain,
        )
        return name

    def _unique_name(self, path: tuple[str, ...]) -> str:
        # A bare list index is meaningless as a name; qualify it with its
        # parent (e.g. ("run", "argv", "1") -> "argv_1").
        if str(path[-1]).isdigit() and len(path) >= 2:
            leaf_parts = path[-2:]
        else:
            leaf_parts = path[-1:]
        leaf = (
            _NAME_RE.sub("_", "_".join(str(p) for p in leaf_parts).lower()).strip("_")
            or "value"
        )
        if leaf not in self._params:
            return leaf
        qualified = _NAME_RE.sub("_", "_".join(path).lower()).strip("_")
        candidate, suffix = qualified, 2
        while candidate in self._params:
            candidate = f"{qualified}_{suffix}"
            suffix += 1
        return candidate

    def parameters(self) -> list[SkillParameter]:
        return list(self._params.values())


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _infer_type(observed: list[Any]) -> tuple[str, str | None]:
    first = observed[0]
    if isinstance(first, bool):
        return "bool", None
    if isinstance(first, int):
        return "int", None
    if isinstance(first, float):
        return "float", None
    if isinstance(first, str):
        if all(isinstance(v, str) and _looks_like_path(v) for v in observed):
            return "path", "path"
        return "str", None
    if isinstance(first, list):
        return "list", None
    if isinstance(first, dict):
        return "dict", None
    return type(first).__name__, None


def _looks_like_path(value: str) -> bool:
    if value.startswith(_WORKSPACE_TOKEN):
        return True
    if "/" in value or "\\" in value:
        return True
    return len(PurePath(value).suffix) > 1


# --------------------------------------------------------------------------- #
# Workspace templating.                                                       #
# --------------------------------------------------------------------------- #
def _workspace_root(demo: Demonstration) -> str | None:
    if demo.before is None:
        return None
    root = demo.before.state.get("workspace")
    return str(root) if root else None


def _template_workspace(value: Any, root: str | None) -> Any:
    """Replace the demonstrated workspace root inside strings with a token."""
    if root is None:
        return value
    if isinstance(value, str):
        if value == root:
            return _WORKSPACE_TOKEN
        for separator in ("/", "\\"):
            prefix = root.rstrip("/\\") + separator
            if value.startswith(prefix):
                return _WORKSPACE_TOKEN + "/" + value[len(prefix) :].replace("\\", "/")
        return value
    if isinstance(value, dict):
        return {k: _template_workspace(v, root) for k, v in value.items()}
    if isinstance(value, list):
        return [_template_workspace(v, root) for v in value]
    return value


def template_demonstration(demo: Demonstration) -> Demonstration:
    """Rewrite a demonstration's action parameters against its workspace root.

    Absolute paths under the demonstrated workspace become ``{workspace}/...``.
    This must run BEFORE PII scrubbing: the scrubber masks every absolute path
    to ``<PATH>``, which would collapse the very variation slot induction
    needs — templated (relative) paths pass the scrubber intact, and the
    user's absolute paths still never enter a stored skill.
    """
    root = _workspace_root(demo)
    if root is None:
        return demo
    return demo.model_copy(
        update={
            "actions": [
                action.model_copy(
                    update={"parameters": _template_workspace(action.parameters, root)}
                )
                for action in demo.actions
            ]
        }
    )


def _common_artifacts(demos: list[Demonstration]) -> list[str]:
    """Artifact paths (workspace-relative) produced by every demonstration."""
    per_demo: list[set[str]] = []
    for demo in demos:
        if demo.before is None or demo.after is None:
            return []
        per_demo.append(set(changed_artifacts(demo.before, demo.after)))
    common = set.intersection(*per_demo) if per_demo else set()
    return sorted(common)


# --------------------------------------------------------------------------- #
# Binding: generalized skill + arguments -> executable actions.               #
# --------------------------------------------------------------------------- #
def bind_parameters(
    skill: ReusableSkill,
    values: dict[str, Any],
    *,
    workspace: str | None = None,
) -> list[ActionEvent]:
    """Substitute slot values (and the workspace root) into a skill's actions.

    Every required parameter must be supplied; a ``{workspace}``-templated
    skill requires ``workspace``. Returns fresh ``ActionEvent``s ready for an
    ``ActionExecutor``.
    """
    missing = [p.name for p in skill.parameters if p.required and p.name not in values]
    if missing:
        raise ValueError("missing parameter(s): " + ", ".join(sorted(missing)))
    known = {p.name for p in skill.parameters}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError("unknown parameter(s): " + ", ".join(unknown))

    def substitute(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {_PARAM_MARKER}:
                return substitute_workspace(values[value[_PARAM_MARKER]])
            return {k: substitute(v) for k, v in value.items()}
        if isinstance(value, list):
            return [substitute(v) for v in value]
        return substitute_workspace(value)

    def substitute_workspace(value: Any) -> Any:
        if isinstance(value, str) and _WORKSPACE_TOKEN in value:
            if workspace is None:
                raise ValueError(
                    "skill is workspace-templated; a workspace is required"
                )
            return value.replace(_WORKSPACE_TOKEN, str(workspace).rstrip("/\\"))
        return value

    return [
        ActionEvent(
            correlation_id=action.correlation_id,
            adapter=action.adapter,
            operation=action.operation,
            parameters=substitute(action.parameters),
            actor=action.actor,
            credential_ref=action.credential_ref,
        )
        for action in skill.actions
    ]
