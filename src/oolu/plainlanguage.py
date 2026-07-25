"""The plain-language gate — B0 of the conversational-building plan.

Mechanisms are the builder's problem; values are the user's. A file
format, an API, an endpoint, a schema is a DECISION the building seat
makes, defaults, and names in its receipt — never a question to a
non-developer who came to get work done. This module is that law made
lexical, in two hands:

- :func:`mechanism_terms` / :func:`mechanism_questions` — the lexicon
  and its reader: which mechanism words a text (or the question
  sentences inside a transcript) trips. The acceptance metric
  ``mechanism_questions_per_build`` reads through here.
- :func:`default_mechanism_parameters` — the wall that RESOLVES
  instead of hiding: an intake parameter whose ask is mechanism-shaped
  is decided on the spot — the model's first suggestion (it was told
  to put its guess there), else the domain's first option — bound with
  ``DERIVED`` provenance and its question retired; with nothing to
  bind, the parameter is demoted to optional. Merely suppressing the
  question would hang the run on a value nobody will ever be asked
  for; deciding is what lets the build proceed, and the receipt names
  what was decided.

The lexicon aims at CHOICES OF MECHANISM (format, schema, endpoint,
encoding, auth), not at values that happen to be technical: asking for
a web address or a folder is a value-ask and passes. A value-ask that
merely mentions a format word ("which folder are the CSVs in") may
trip — that is the accepted cost, priced in the plan: the build then
proceeds on a default instead of asking, which is the LAW's preferred
failure direction.
"""

from __future__ import annotations

import re

# Choice-of-mechanism vocabulary. Multi-word phrases first (matched as
# phrases); single words hold a hard word boundary so "auth" never
# fires inside "author".
MECHANISM_LEXICON: tuple[str, ...] = (
    "file format",
    "file type",
    "data type",
    "content type",
    "format",
    "extension",
    "encoding",
    "mime",
    "schema",
    "api",
    "apis",
    "endpoint",
    "endpoints",
    "protocol",
    "json",
    "xml",
    "yaml",
    "csv",
    "sql",
    "regex",
    "delimiter",
    "token",
    "tokens",
    "oauth",
    "auth",
    "authentication",
    "header",
    "headers",
    "webhook",
    "webhooks",
    "payload",
    "sdk",
    "library",
    "framework",
    "parser",
    "database",
)

_TERM_RES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (
        term,
        re.compile(
            r"(?<![a-z0-9])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![a-z0-9])",
            re.IGNORECASE,
        ),
    )
    for term in MECHANISM_LEXICON
)


def mechanism_terms(text: str | None) -> list[str]:
    """The lexicon terms a text trips, in lexicon order, deduplicated
    by position of first appearance — empty means plain language."""
    if not text:
        return []
    hits: list[str] = []
    for term, pattern in _TERM_RES:
        if pattern.search(text) and not any(term in h for h in hits):
            hits.append(term)
    return hits


def is_mechanism_ask(*parts: str | None) -> bool:
    """Whether ANY of the given texts (a question, a parameter name,
    its description) reads as a choice of mechanism."""
    return any(mechanism_terms(part) for part in parts)


def mechanism_questions(text: str | None) -> list[str]:
    """The QUESTION sentences inside a transcript that trip the
    lexicon — statements are free to name mechanisms (a receipt should:
    "I'll read it as a spreadsheet"), only questions are budgeted.
    This is the reader the acceptance metric counts through."""
    if not text:
        return []
    offenders: list[str] = []
    for chunk in re.split(r"(?<=[?？])", text):
        sentence = chunk.strip()
        if not sentence.endswith(("?", "？")):
            continue
        # The question is the tail clause, not everything since the
        # last question mark — a receipt sentence followed by a short
        # question must not lend it its vocabulary.
        tail = re.split(r"[.!;\n]", sentence)[-1].strip() or sentence
        if mechanism_terms(tail):
            offenders.append(tail)
    return offenders


def default_mechanism_parameters(brief):
    """Resolve every mechanism-shaped ask in a brief; return
    ``(brief, decisions)``.

    A parameter still unresolved whose name, description, or question
    trips the lexicon is DECIDED here: the first suggested value (the
    intake model is instructed to put its guess there), else the
    domain's first option, bound with ``DERIVED`` provenance and the
    question retired. With nothing to bind, the parameter is demoted
    to optional — a mechanism nobody can ask about must never block a
    run. ``decisions`` names each call taken, for the receipt:
    ``[{"parameter", "value"}]`` with ``value=None`` for demotions."""
    from .skills.requirements import ParameterSource

    changed: list[dict] = []
    parameters = []
    for param in brief.parameters:
        undecided = param.value is None and param.required
        if not undecided or not is_mechanism_ask(
            param.question, param.name, param.description
        ):
            parameters.append(param)
            continue
        choice = None
        if param.suggested_values:
            choice = param.suggested_values[0]
        elif param.domain.options:
            choice = param.domain.options[0]
        if choice is not None:
            parameters.append(
                param.model_copy(
                    update={
                        "value": choice,
                        "source": ParameterSource.DERIVED,
                        "question": None,
                    }
                )
            )
        else:
            parameters.append(
                param.model_copy(update={"required": False, "question": None})
            )
        changed.append({"parameter": param.name, "value": choice})
    if not changed:
        return brief, []
    return brief.model_copy(update={"parameters": parameters}), changed
