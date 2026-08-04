"""The desks as nodes (N7) — the magazine company as a paved web.

Each desk of the news pipeline is defined here as an ordinary NODE
CONTRACT in the platform's one vocabulary: typed slots in, typed slots
out, a deterministic script body. The Paver — the standing machinery,
untouched — surveys them, derives the edges FROM the slot matches
(never narrated), projects the tenant's editorial SOP onto them as
guard edges, rehearses the whole web in the severed sandbox, and
promotes it to one node under the News agent's principal (P4). One
trigger (the morning pulse on the genre desk) then fires the whole
web; evolution is publish-successor → the promotion retires the
predecessor and re-paves (P2).

**The slot vocabulary** (roadmap Part III — a synonym is a different
universe, so these names are constants, never retyped inline):

    press_engagement_report  # metrics spine → genre desk (the loop closes)
    press_genre_demand       # genre desk  → topic desk
    press_topic_brief        # topic desk  → research desk
    press_research_bundle    # research desk → survey desk
    press_survey_result      # survey desk → composition desk
    press_post_draft         # composition → publication
    press_post_published     # publication's effect

**The dataflow carries the day's readings.** The web is a DATAFLOW: the
anchor consumes the day's typed readings bundle (engagement + beat +
draw seed — filed by the metrics spine, the web's boundary input) and
each desk passes forward exactly what downstream needs. No desk script
reaches a store: store I/O lives at the boundary and in the desks'
LIBRARY code (doctrine 4 — fully deterministic store work opts out of
the route), so every script rehearses in the severed sandbox.

**Sampled decisions stay auditable** (doctrine 2): the genre desk's
script Thompson-samples the completion posterior with the DRAW SEED the
report carries — stored inputs + stored seed = the same reading, in the
sandbox and in production alike.

**The editorial law is authored, not implied**: :data:`EDITORIAL_SOP`
is the default law the contribution door files into the tenant SOP
store (P1) under the OWNER's name — the survey-desk guard (a survey
that tests nothing never runs) and the publication guard (no resolved
sources, no post). The Paver refuses to pave the web unguarded if the
rules apply and cannot be expressed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..skills.contract import Slot
from ..skills.models import ActionEvent, ReusableSkill, SkillSignature

DESKNODES_VERSION = 1

# The slot vocabulary — exact names, exact types (node-generation §3).
SLOT_ENGAGEMENT_REPORT = "press_engagement_report"
SLOT_GENRE_DEMAND = "press_genre_demand"
SLOT_TOPIC_BRIEF = "press_topic_brief"
SLOT_RESEARCH_BUNDLE = "press_research_bundle"
SLOT_SURVEY_RESULT = "press_survey_result"
SLOT_POST_DRAFT = "press_post_draft"
SLOT_POST_PUBLISHED = "press_post_published"

_VALUE_TYPE = "json"


def _slot(name: str) -> dict:
    return {"name": name, "value_type": _VALUE_TYPE, "role": None}


@dataclass(frozen=True)
class DeskNode:
    """One desk, node-shaped: the name the SOP and the pulse address it
    by, its slot contract, and its deterministic script."""

    name: str
    summary: str
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    script: str

    def skill(self) -> ReusableSkill:
        """The contributable skill — the sole-script-action shape every
        function node publishes (fileable producer, effect-free)."""
        return ReusableSkill(
            name=self.name,
            description=self.summary,
            signature=SkillSignature(application="script", adapter="script"),
            actions=[
                ActionEvent(
                    correlation_id="function",
                    adapter="script",
                    operation="run",
                    parameters={
                        "goal": self.name,
                        "script": self.script,
                        "node_key": self.name,
                    },
                )
            ],
        )

    def slot_consumes(self) -> list[Slot]:
        return [Slot(**_slot(name)) for name in self.consumes]

    def slot_produces(self) -> list[Slot]:
        return [Slot(**_slot(name)) for name in self.produces]


# --------------------------------------------------------------------------- #
# The scripts — bindings.json in, emit_result out (the adapter contract).      #
# Deterministic over their inputs; the sampled reading is seeded by the        #
# report's recorded draw seed, so it replays exactly.                          #
# --------------------------------------------------------------------------- #
_GENRE_SCRIPT = '''"""The genre desk: which genre are readers leaning toward, on evidence.

Thompson-samples the completion posterior per genre with the report's
recorded draw seed (auditable stochasticity: stored inputs + stored
seed = the same reading), blended with interest taps and live supply.
"""
import json
import random

from _oolu_runtime import emit_error, emit_result

with open("bindings.json", encoding="utf-8") as _fh:
    report = json.load(_fh).get("press_engagement_report")
if not isinstance(report, dict):
    emit_error("no engagement report bound", kind="MissingInput")
else:
    seed = int(report.get("draw_seed") or 0)
    genres = report.get("genres") or {}
    scored = []
    for genre in sorted(genres):
        row = genres.get(genre) or {}
        opens = max(0, int(row.get("opens") or 0))
        done = min(opens, max(0, int(row.get("completions") or 0)))
        rng = random.Random(seed + sum(map(ord, genre)))
        engagement = rng.betavariate(done + 1, opens - done + 1)
        taps = int(row.get("taps") or 0)
        pieces = int(row.get("pieces") or 0)
        interest = taps / (taps + 3)
        supply = min(1.0, pieces / 4)
        scored.append((0.5 * engagement + 0.3 * interest + 0.2 * supply, genre))
    scored.sort(reverse=True)
    if not scored:
        emit_error("the report names no genres", kind="BadInput")
    else:
        emit_result({"press_genre_demand": {
            "chosen": scored[0][1],
            "ranked": [g for _, g in scored],
            "draw_seed": seed,
            "beat": report.get("beat") or [],
            "sample": report.get("sample") or [],
        }})
'''

_TOPIC_SCRIPT = '''"""The topic desk: the day's topic from the chosen genre's beat rows."""
import json

from _oolu_runtime import emit_error, emit_result

with open("bindings.json", encoding="utf-8") as _fh:
    demand = json.load(_fh).get("press_genre_demand")
if not isinstance(demand, dict):
    emit_error("no demand reading bound", kind="MissingInput")
else:
    rows = [r for r in (demand.get("beat") or []) if isinstance(r, dict)]
    ranked = {g: i for i, g in enumerate(demand.get("ranked") or [])}
    best, best_score = None, -1.0
    for row in rows:
        facts = [f for f in (row.get("facts") or []) if isinstance(f, dict)]
        if not facts:
            continue  # a topic with no typed facts is not a topic
        genre_rank = ranked.get(str(row.get("genre") or ""), len(ranked))
        score = (len(ranked) - genre_rank) + 0.1 * len(facts)
        if score > best_score:
            best, best_score = row, score
    if best is None:
        emit_error("no evidenced beat row to tell", kind="BadInput")
    else:
        emit_result({"press_topic_brief": {
            "topic_key": str(best.get("topic_key") or ""),
            "subject": str(best.get("subject") or ""),
            "facts": best.get("facts") or [],
            "disclosure": str(best.get("disclosure") or ""),
            "sample": demand.get("sample") or [],
        }})
'''

_RESEARCH_SCRIPT = '''"""The research desk: verify the brief's facts, name what stays open.

Every fact must be typed (kind, ref, summary) — a malformed row is
DROPPED and counted against sources_resolved; a measured disagreement
becomes a named open question the survey may test. No invention: the
bundle holds exactly what the records carry.
"""
import json

from _oolu_runtime import emit_error, emit_result

with open("bindings.json", encoding="utf-8") as _fh:
    brief = json.load(_fh).get("press_topic_brief")
if not isinstance(brief, dict):
    emit_error("no topic brief bound", kind="MissingInput")
else:
    facts = []
    for fact in brief.get("facts") or []:
        if isinstance(fact, dict) and fact.get("kind") and fact.get("ref") \\
                and fact.get("summary"):
            facts.append({"kind": str(fact["kind"]), "ref": str(fact["ref"]),
                          "summary": str(fact["summary"])})
    open_questions = []
    kinds = {f["kind"] for f in facts}
    if {"lab", "feedback"} <= kinds:
        open_questions.append("do the measured results match the lived ones?")
    if "price" in kinds:
        open_questions.append("is it worth the asking price?")
    if not facts:
        emit_error("no fact survived verification — nothing to research",
                   kind="BadInput")
    else:
        emit_result({"press_research_bundle": {
            "topic_key": str(brief.get("topic_key") or ""),
            "subject": str(brief.get("subject") or ""),
            "facts": facts,
            "sources_resolved": len(facts),
            "open_questions": len(open_questions),
            "questions": open_questions,
            "disclosure": str(brief.get("disclosure") or ""),
            "sample": brief.get("sample") or [],
        }})
'''

_SURVEY_SCRIPT = '''"""The survey desk: the instrument for the bundle's first open question.

Composes the question and the closed option set for the drawn sample —
the standing survey desk (library code) fields the answers under the
k-anonymity floor; this node decides WHAT to ask and WHOM.
"""
import json

from _oolu_runtime import emit_error, emit_result

with open("bindings.json", encoding="utf-8") as _fh:
    bundle = json.load(_fh).get("press_research_bundle")
if not isinstance(bundle, dict):
    emit_error("no research bundle bound", kind="MissingInput")
else:
    questions = bundle.get("questions") or []
    emit_result({"press_survey_result": {
        "topic_key": str(bundle.get("topic_key") or ""),
        "question": str(questions[0]) if questions else "",
        "options": ["worth", "not_worth", "more_evidence"],
        "sample": bundle.get("sample") or [],
        "brief": bundle,
    }})
'''

_COMPOSITION_SCRIPT = '''"""The composition desk: the final post — typed facts, notary's records.

The desk post: facts rendered plainly, the survey line included, the
disclosure appended VERBATIM (law 3), the full source table beside the
prose. sources_resolved rides the result so the publication guard
judges it.
"""
import json

from _oolu_runtime import emit_error, emit_result

with open("bindings.json", encoding="utf-8") as _fh:
    survey = json.load(_fh).get("press_survey_result")
brief = (survey or {}).get("brief") if isinstance(survey, dict) else None
if not isinstance(brief, dict):
    emit_error("no survey result (with brief snapshot) bound",
               kind="MissingInput")
else:
    facts = brief.get("facts") or []
    sources = list(facts)
    if survey.get("question"):
        sources.append({"kind": "survey",
                        "ref": str(survey.get("topic_key") or ""),
                        "summary": str(survey.get("question") or "")})
    prose = " ".join(str(f.get("summary") or "") for f in facts)
    disclosure = str(brief.get("disclosure") or "")
    if disclosure and disclosure not in prose:
        prose = (prose + " " + disclosure).strip()
    emit_result({"press_post_draft": {
        "topic_key": str(brief.get("topic_key") or ""),
        "headline": str(brief.get("subject") or "")[:140],
        "prose": prose,
        "sources": sources,
        "sources_resolved": len(sources),
        "disclosure": disclosure,
    }})
'''

_PUBLICATION_SCRIPT = '''"""The publication desk: the push decision, exactly-once by receipt.

Decides the post publishes and hands the standing publication library
(match_edition + delivery receipts) the typed record it lands with —
the web's effect slot, applied at the boundary.
"""
import json

from _oolu_runtime import emit_error, emit_result

with open("bindings.json", encoding="utf-8") as _fh:
    draft = json.load(_fh).get("press_post_draft")
if not isinstance(draft, dict):
    emit_error("no post draft bound", kind="MissingInput")
elif not draft.get("headline") or not draft.get("prose"):
    emit_error("a post needs a headline and prose", kind="BadInput")
else:
    emit_result({"press_post_published": {
        "topic_key": str(draft.get("topic_key") or ""),
        "headline": str(draft.get("headline") or ""),
        "prose": str(draft.get("prose") or ""),
        "sources": draft.get("sources") or [],
        "disclosure": str(draft.get("disclosure") or ""),
        "publish": True,
    }})
'''


# The desk names ARE addresses: the SOP's guard patterns and the pulse
# schedule's goal resolve them case-folded, so they are constants too.
GENRE_DESK = "press genre desk"
TOPIC_DESK = "press topic desk"
RESEARCH_DESK = "press research desk"
SURVEY_DESK = "press survey desk"
COMPOSITION_DESK = "press composition desk"
PUBLICATION_DESK = "press publication desk"

DESK_NODES: tuple[DeskNode, ...] = (
    DeskNode(
        name=GENRE_DESK,
        summary="rank reader demand over the day's engagement report",
        consumes=(SLOT_ENGAGEMENT_REPORT,),
        produces=(SLOT_GENRE_DEMAND,),
        script=_GENRE_SCRIPT,
    ),
    DeskNode(
        name=TOPIC_DESK,
        summary="choose the day's topic from the chosen genre's beat",
        consumes=(SLOT_GENRE_DEMAND,),
        produces=(SLOT_TOPIC_BRIEF,),
        script=_TOPIC_SCRIPT,
    ),
    DeskNode(
        name=RESEARCH_DESK,
        summary="verify the brief's facts and name the open questions",
        consumes=(SLOT_TOPIC_BRIEF,),
        produces=(SLOT_RESEARCH_BUNDLE,),
        script=_RESEARCH_SCRIPT,
    ),
    DeskNode(
        name=SURVEY_DESK,
        summary="compose the survey instrument for the first open question",
        consumes=(SLOT_RESEARCH_BUNDLE,),
        produces=(SLOT_SURVEY_RESULT,),
        script=_SURVEY_SCRIPT,
    ),
    DeskNode(
        name=COMPOSITION_DESK,
        summary="compose the final post from facts and the survey line",
        consumes=(SLOT_SURVEY_RESULT,),
        produces=(SLOT_POST_DRAFT,),
        script=_COMPOSITION_SCRIPT,
    ),
    DeskNode(
        name=PUBLICATION_DESK,
        summary="decide the push and land the typed publication record",
        consumes=(SLOT_POST_DRAFT,),
        produces=(SLOT_POST_PUBLISHED,),
        script=_PUBLICATION_SCRIPT,
    ),
)


# The default editorial law (Part III), filed into the tenant SOP store
# (P1) under the OWNER's name when the desks are contributed. Guards
# read the producer's filed evidence at ``result/...`` — the same
# pointer the value pipe stores payloads under.
EDITORIAL_SOP = """\
sop: editorial-law
require_guard:
  - operation: 'press survey desk*'
    source: 'press research desk*'
    when: {pointer: result/press_research_bundle/open_questions, op: '>', value: 0}
  - operation: 'press publication desk*'
    source: 'press composition desk*'
    when: {pointer: result/press_post_draft/sources_resolved, op: '>', value: 0}
"""
