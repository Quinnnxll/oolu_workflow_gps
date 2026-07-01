"""Model-backed natural-language intake.

Turns a free-text ``TaskContract.intent`` into a structured ``RequirementBrief`` —
the parameters, their domains, and the clarifying questions the Requirement and
Constraint Compiler needs to decide whether a run can proceed. This is the
model-backed adapter that ``StaticIntaker`` (test-only, pre-built brief) always
pointed at (see ``docs/ADAPTER_MATURITY.md``).

Three commitments are carried over from the rest of the system, and they are the
reason this is more than a JSON call:

  1. NO SILENT BINDING. The intaker may *suggest* values and *ask* questions, but
     it never binds a parameter ``value``. Resolution carries provenance
     (``USER``/``DERIVED``/``AUTHORITY``) and happens later, through the compiler
     and the desktop/gateway resume path. A model that guesses a value only
     populates ``suggested_values`` — the selection is still the human's.

  2. THE MODEL NEVER SELF-AUTHORIZES. Authorization mode (how much the agent may
     decide on its own) is a human/policy decision, never the model's. Any
     ``authorization`` the model emits is ignored; a brief from intake is always
     ``GUIDED``. Widening authority is an explicit, out-of-band act.

  3. WORKS WITH NO NETWORK. Like ``NoopKnowledgeClient`` and ``FakeGateway``,
     intake degrades to a deterministic offline path. ``HeuristicIntaker``
     produces a usable brief from the raw intent alone, and ``ModelBackedIntaker``
     falls back to it whenever no model is configured or the model's answer is
     unusable — a bad answer is not an error (mirrors ``routing.gateway``'s "no
     code in the completion is not a ``GatewayError``").
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol, runtime_checkable

from ..skills.models import ConstraintSeverity, ConstraintSpec
from ..skills.requirements import (
    AuthorizationGrant,
    AuthorizationMode,
    ParameterDomain,
    RequirementBrief,
    RequirementParameter,
)
from .state import TaskContract

logger = logging.getLogger(__name__)

# A fenced ```json block, else the outermost brace-balanced object. Models answer
# in prose-plus-JSON far more reliably than in a bare object, so we extract.
_JSON_FENCE_RE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n(.*?)```", re.DOTALL)


# --------------------------------------------------------------------------- #
# Model seam.                                                                  #
# --------------------------------------------------------------------------- #
@runtime_checkable
class IntakeModel(Protocol):
    """Propose a structured brief for an intent, as JSON text.

    Deliberately narrow and distinct from the code-synthesis ``routing.Gateway``:
    intake wants structured parameters, not an extracted Python script. The return
    value is raw text (the model may wrap the JSON in prose or a fence); parsing
    and every safety guard live in :class:`ModelBackedIntaker`, never here.
    """

    def propose(self, intent: str) -> str: ...


# The instruction handed to a live model. Kept explicit so the contract the parser
# enforces (no bound values, suggestions only, questions for what is missing) is
# also what the model is asked for.
INTAKE_SYSTEM_PROMPT = (
    "You turn a user's request into a structured brief. Reply with a single JSON "
    "object and nothing else, shaped as:\n"
    '{"parameters": [{"name": str, "description": str, "value_type": '
    '"string"|"number"|"boolean", "unit": str|null, "required": bool, '
    '"options": [..], "suggested_values": [..], "question": str, '
    '"question_priority": int}], "constraints": [{"id": str, '
    '"description": str, "validator": str, "severity": "hard"|"soft"}]}\n'
    "Do NOT select values. Put any value you would guess into suggested_values "
    "only, and phrase a question for it. Ask a question for every parameter the "
    "user has not already pinned down. Omit fields you are unsure of."
)


class LiteLLMIntakeModel:
    """Production ``IntakeModel`` over LiteLLM (any OpenAI-compatible endpoint).

    Lazily imports LiteLLM (behind the ``engine`` extra) so this module imports
    without it, exactly like ``routing.gateway.LiteLLMGateway``. Credentials and
    the endpoint come from the environment, never persisted here.
    """

    def __init__(self, model: str, *, temperature: float = 0.0, timeout: float = 60.0):
        try:
            import litellm
        except (
            ImportError
        ) as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "litellm not installed (`pip install 'workflow-gps[engine]'`)"
            ) from exc
        self._litellm = litellm
        litellm.drop_params = True
        self._model = model
        self._temperature = temperature
        self._timeout = timeout

    def propose(self, intent: str) -> str:
        response = self._litellm.completion(
            model=self._model,
            temperature=self._temperature,
            timeout=self._timeout,
            messages=[
                {"role": "system", "content": INTAKE_SYSTEM_PROMPT},
                {"role": "user", "content": intent},
            ],
        )
        return getattr(response.choices[0].message, "content", None) or ""


# --------------------------------------------------------------------------- #
# Offline fallback.                                                            #
# --------------------------------------------------------------------------- #
class HeuristicIntaker:
    """Deterministic, no-network intake.

    It invents nothing: the brief carries the intent verbatim with no parameters
    and ``GUIDED`` authorization. The run proceeds to grounding on the raw intent
    rather than fabricating structure the user never stated. This is the honest
    floor the system runs on when no model is available.
    """

    def intake(self, contract: TaskContract) -> RequirementBrief:
        return RequirementBrief(
            intent=contract.intent,
            parameters=[],
            constraints=[],
            authorization=AuthorizationGrant(mode=AuthorizationMode.GUIDED),
        )


# --------------------------------------------------------------------------- #
# Model-backed intake.                                                         #
# --------------------------------------------------------------------------- #
class ModelBackedIntaker:
    """Intake via a language model, degrading to a deterministic brief.

    The model's answer is parsed defensively: unparseable output, a transport
    failure, or a wholly malformed object all fall back to ``HeuristicIntaker``
    (the run is never killed by a bad model turn), and individual malformed
    parameters/constraints are dropped rather than aborting the whole brief.
    """

    def __init__(
        self,
        model: IntakeModel | None = None,
        *,
        fallback: HeuristicIntaker | None = None,
    ):
        self._model = model
        self._fallback = fallback or HeuristicIntaker()

    def intake(self, contract: TaskContract) -> RequirementBrief:
        if self._model is None:
            return self._fallback.intake(contract)
        try:
            text = self._model.propose(contract.intent)
        except Exception:  # noqa: BLE001 - a failed model turn degrades, never crashes
            logger.warning(
                "intake model failed; using heuristic fallback", exc_info=True
            )
            return self._fallback.intake(contract)
        brief = _parse_brief(contract.intent, text)
        return brief if brief is not None else self._fallback.intake(contract)


# --------------------------------------------------------------------------- #
# Parsing — pure and defensive.                                               #
# --------------------------------------------------------------------------- #
def _extract_json(text: str | None) -> dict[str, Any] | None:
    """Pull a JSON object out of a completion. ``None`` if there is none."""
    if not text:
        return None
    candidates: list[str] = []
    fenced = _JSON_FENCE_RE.findall(text)
    candidates.extend(block.strip() for block in fenced if block.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_brief(intent: str, text: str | None) -> RequirementBrief | None:
    data = _extract_json(text)
    if data is None:
        return None
    parameters = [
        param
        for raw in _as_list(data.get("parameters"))
        if (param := _parse_parameter(raw)) is not None
    ]
    constraints = [
        constraint
        for raw in _as_list(data.get("constraints"))
        if (constraint := _parse_constraint(raw)) is not None
    ]
    # Authorization is never taken from the model: a brief from intake is GUIDED.
    return RequirementBrief(
        intent=intent,
        parameters=parameters,
        constraints=constraints,
        authorization=AuthorizationGrant(mode=AuthorizationMode.GUIDED),
    )


def _parse_parameter(raw: Any) -> RequirementParameter | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        domain = ParameterDomain(
            value_type=str(raw.get("value_type", "string")),
            unit=_opt_str(raw.get("unit")),
            minimum=_opt_float(raw.get("minimum")),
            maximum=_opt_float(raw.get("maximum")),
            options=_as_list(raw.get("options")),
        )
        # `value`/`source` are intentionally never read: the model does not bind
        # values, it only suggests them. Resolution happens later with provenance.
        return RequirementParameter(
            name=name.strip(),
            description=str(raw.get("description", "")),
            domain=domain,
            required=bool(raw.get("required", True)),
            suggested_values=_as_list(raw.get("suggested_values")),
            question=_opt_str(raw.get("question")),
            question_priority=_opt_priority(raw.get("question_priority")),
        )
    except (ValueError, TypeError):
        return None


def _parse_constraint(raw: Any) -> ConstraintSpec | None:
    if not isinstance(raw, dict):
        return None
    cid = raw.get("id")
    if not isinstance(cid, str) or not cid.strip():
        return None
    severity = (
        ConstraintSeverity.SOFT
        if str(raw.get("severity", "hard")).lower() == "soft"
        else ConstraintSeverity.HARD
    )
    try:
        return ConstraintSpec(
            id=cid.strip(),
            description=str(raw.get("description", "")),
            validator=str(raw.get("validator", "")),
            severity=severity,
        )
    except (ValueError, TypeError):
        return None


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool):  # bool is an int subclass; never a bound
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _opt_priority(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))
