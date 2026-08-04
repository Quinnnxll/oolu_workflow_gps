"""The composition desk (N4) — the final post: reporter's voice, notary's records.

A post is composed from exactly three typed inputs: the topic brief's
evidence rows (the research bundle N2 mined), the survey's floored
result row (N3's opinion aggregate), and the cited contributions
resolved live. The laws, inherited from the newsroom and extended:

- **The source table is the contract.** Every source row the post was
  composed from is stored beside it (`press_story_sources`) and the
  rendered "Sources" section is exactly those rows — the stored table
  and the rendered table cannot diverge because they are one table.
  A post with no lineage AND no sources cannot be stored (the
  insert-refusal law, N4-extended).
- **No invention, with a desk fallback.** The seat's model may voice
  the post under a hard output contract (HEADLINE:/PROSE:, facts from
  the numbered sources only); a broken contract or a dead model falls
  back to the DESK POST — the typed facts rendered plainly, the survey
  line included, publishable on a host with no brain at all.
- **The disclosure survives verbatim (law 3).** The brief's disclosure
  is carried on the story row and APPENDED to the prose by the desk —
  never left to the model's discretion, never trimmed downstream.
- **Contributors stay principals.** Contribution sources also produce
  lineage shares (an equal split summing exactly to 1.0), so the ad
  dividend keeps paying the members whose pieces anchored the post.
"""

from __future__ import annotations

from typing import Callable, Mapping, Sequence

from .newsroom import MAX_HEADLINE_CHARS, LineageShare

COMPOSER_VERSION = 1

_POST_FRAME = """\
You are the composition desk of a member magazine reporting on its own
marketplace. Compose ONE short news post from ONLY the numbered sources
below. Hard rules:
- Every fact must come from a numbered source. Never invent a detail,
  a number, a quote, or an opinion.
- Report unfavorable numbers exactly as recorded — honesty cuts both
  ways.
- Reply in EXACTLY this shape, nothing else:
HEADLINE: <one line>
PROSE: <one short paragraph>
"""


def _survey_line(result: Mapping | None) -> str:
    """The survey aggregate in words — floored: counts when revealed,
    the honest reason otherwise, never an individual answer."""
    if not result:
        return ""
    answers = int(result.get("answers") or 0)
    counts = result.get("counts")
    if counts:
        parts = ", ".join(
            f"{key}: {value}" for key, value in sorted(counts.items())
        )
        return f"Reader survey ({answers} answers): {parts}."
    return f"Reader survey: {result.get('reason') or 'no answers yet'}."


def source_rows(
    brief: Mapping, survey_result: Mapping | None
) -> list[dict]:
    """The post's source table: every brief fact plus the survey's
    result row — typed, ref'd, worded. The stored table and the
    rendered table are these rows exactly."""
    rows = [
        {
            "kind": str(fact.get("kind") or ""),
            "ref": str(fact.get("ref") or ""),
            "summary": str(fact.get("summary") or ""),
        }
        for fact in (brief.get("facts") or [])
        if isinstance(fact, Mapping)
    ]
    if survey_result:
        rows.append(
            {
                "kind": "survey",
                "ref": str(survey_result.get("survey_id") or ""),
                "summary": (
                    f"{survey_result.get('question') or ''} — "
                    + _survey_line(survey_result)
                ).strip(" —"),
            }
        )
    return rows


def lineage_from(
    brief: Mapping,
    contribution_of: Callable[[str], object | None],
) -> list[LineageShare]:
    """Contribution sources as lineage shares — an equal split summing
    exactly to 1.0 (the last share absorbs rounding), resolved LIVE so
    an unpublished piece honestly drops out. Empty when the post cites
    no member pieces: a pure-market post has sources, not lineage."""
    resolved = []
    seen: set[str] = set()
    for fact in brief.get("facts") or []:
        if not isinstance(fact, Mapping) or fact.get("kind") != "contribution":
            continue
        ref = str(fact.get("ref") or "")
        if not ref or ref in seen:
            continue
        record = contribution_of(ref)
        if record is None:
            continue
        seen.add(ref)
        resolved.append(record)
    if not resolved:
        return []
    share = round(1.0 / len(resolved), 6)
    shares = [
        LineageShare(
            contribution_id=record.contribution_id,
            author=record.author,
            weight=share,
        )
        for record in resolved[:-1]
    ]
    last = resolved[-1]
    shares.append(
        LineageShare(
            contribution_id=last.contribution_id,
            author=last.author,
            weight=round(1.0 - share * (len(resolved) - 1), 6),
        )
    )
    return shares


def desk_post(
    brief: Mapping, survey_result: Mapping | None
) -> tuple[str, str]:
    """The model-less composition: the typed facts rendered plainly —
    degraded is honest; invented is a defect."""
    headline = str(brief.get("subject") or "")[:MAX_HEADLINE_CHARS]
    lines = [
        str(fact.get("summary") or "")
        for fact in (brief.get("facts") or [])
        if isinstance(fact, Mapping) and fact.get("summary")
    ]
    survey = _survey_line(survey_result)
    if survey:
        lines.append(survey)
    return headline, " ".join(lines)


def _parse_post(raw: str) -> tuple[str, str]:
    """The output contract, enforced: anything off-shape composes
    nothing and the desk speaks instead."""
    headline = prose = ""
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("HEADLINE:") and not headline:
            headline = stripped[len("HEADLINE:") :].strip()
        elif stripped.startswith("PROSE:") and not prose:
            prose = stripped[len("PROSE:") :].strip()
    if not headline or not prose:
        return "", ""
    return headline[:MAX_HEADLINE_CHARS], prose


def compose_post(
    brief: Mapping,
    survey_result: Mapping | None,
    *,
    model=None,
) -> tuple[str, str, str]:
    """``(headline, prose, source)`` — the seat's voice under the hard
    contract when a model stands, the desk's plain facts otherwise.
    The DISCLOSURE is appended by the caller's store row and the desk,
    never entrusted to the model."""
    if model is not None:
        numbered = "\n".join(
            f"S{i + 1}. [{row['kind']}] {row['summary']}"
            for i, row in enumerate(source_rows(brief, survey_result))
        )
        prompt = (
            f"{_POST_FRAME}\nSubject: {brief.get('subject') or ''}\n"
            f"Sources:\n{numbered}"
        )
        try:
            from ..chat import ModelBudgetExceeded, ModelUnavailable

            try:
                raw = model.reply([{"role": "user", "content": prompt}])
            except (ModelUnavailable, ModelBudgetExceeded):
                raw = ""
        except ImportError:  # a stripped host: the desk still speaks
            raw = ""
        headline, prose = _parse_post(raw)
        if headline and prose:
            return headline, prose, "model"
    headline, prose = desk_post(brief, survey_result)
    return headline, prose, "desk"


def compose_story_parts(
    brief: Mapping,
    survey_result: Mapping | None,
    *,
    contribution_of: Callable[[str], object | None],
    model=None,
) -> dict:
    """Everything the caller stores, in one bundle: headline/prose/
    source (voice), the lineage shares, the source table, and the
    disclosure carried VERBATIM from the brief. The prose always ends
    with the disclosure when one exists — law 3 is the desk's hand,
    not the model's choice."""
    headline, prose, voice = compose_post(
        brief, survey_result, model=model
    )
    disclosure = str(brief.get("disclosure") or "")
    if disclosure and disclosure not in prose:
        prose = f"{prose} {disclosure}".strip()
    return {
        "headline": headline,
        "prose": prose,
        "source": voice,
        "lineage": lineage_from(brief, contribution_of),
        "sources": source_rows(brief, survey_result),
        "disclosure": disclosure,
    }


def sources_summary(rows: Sequence[Mapping]) -> str:
    """The "Sources" section in words — one line per stored row."""
    return "\n".join(
        f"[{row.get('kind')}] {row.get('summary')}" for row in rows
    )
