"""Org templates: a Supernode imports a working structure, deterministically.

A Supernode describes itself in a sentence; the template button turns that
description into member nodes — each with a NAME, one clear RESPONSIBILITY,
and an essential starting FUNCTION — arranged as a lean organization.

Two convictions shape this module:

- **Deterministic plan first, reasoning last.** Same as node execution: the
  route is assembled from evidence, and a model is consulted only when the
  evidence is thin. The catalog below is the evidence — a curated library
  of working structures. Matching a description to a template is pure
  keyword arithmetic; the SAME description always resolves to the SAME
  template, and once a Supernode's choice is recorded it is never
  re-reasoned — pressing the button twice never burns a second thought.
  The model's only job, when matching fails, is to PICK a key from this
  catalog — it never invents an org chart free-hand.

- **Lean beats large.** A big corporation or a government division does
  not get a bigger template — it gets a LEANER one, because communication,
  coordination, trust, and clear responsibility are what actually limit
  mass-produced intelligence. Every template stays within
  ``MAX_TEMPLATE_ROLES`` roles, and every role answers for exactly one
  thing. Scale comes from each role's node growing its function, not from
  adding chairs to the meeting.

Each role's essential function is deterministic: a script that emits the
role's structured work product (its record fields and working checklist) —
the honest starting step a route can chain on today, grown later by
rebuilding the node with a model when the org wants more.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict

# The leanness wall: no template — corporation, ministry, or garage —
# seats more roles than this.
MAX_TEMPLATE_ROLES = 5

# How many distinct keyword hits count as ENOUGH EVIDENCE of a good
# structure. Below this, the description hasn't earned a deterministic
# verdict and the model may be consulted (to pick, never to invent).
MATCH_EVIDENCE_THRESHOLD = 2


class RoleSpec(BaseModel):
    """One seat in the org: a name, one responsibility, one function."""

    model_config = ConfigDict(frozen=True)

    name: str
    # ONE sentence: what this role ANSWERS FOR. Clear responsibility is
    # the whole point — it is what keeps the org lean.
    responsibility: str
    # The essential function as an executable sentence — the goal the
    # node is built for (also its registry summary).
    goal: str
    # The record fields this role fills on every pass of its work.
    fields: tuple[str, ...]
    # The working checklist the role runs, in order.
    checklist: tuple[str, ...]
    # Authority under the Supernode (1-5). The intake/coordinating seat
    # carries 2; every other seat carries 1.
    authority: int = 1


class OrgTemplate(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    # Why this structure works — shown to the human before importing.
    purpose: str
    # Lowercase single words matched against the Supernode's description.
    keywords: tuple[str, ...]
    roles: tuple[RoleSpec, ...]


def role_script(role: RoleSpec) -> str:
    """The role's essential function: a deterministic, self-contained
    script that emits the role's structured work product — its record
    (fields blank, ready to fill) and its checklist. No model writes
    this; the template IS the plan. The node grows from here: rebuild
    it with a model when the org wants the function to do more."""
    product = {
        "role": role.name,
        "responsibility": role.responsibility,
        "record": {field: "" for field in role.fields},
        "checklist": [
            {"step": step, "done": False} for step in role.checklist
        ],
    }
    payload = json.dumps(json.dumps(product, ensure_ascii=False))
    return (
        f'"""{role.name} — {role.responsibility}"""\n'
        "import json\n"
        "from _oolu_runtime import emit_result\n"
        "\n"
        "# The role's work product: its record and checklist, emitted\n"
        "# blank and structured so the next node on the route can fill\n"
        "# and check it. Deterministic by design.\n"
        f"WORK_PRODUCT = json.loads({payload})\n"
        "\n"
        "emit_result(WORK_PRODUCT)\n"
    )


# --------------------------------------------------------------------------- #
# The catalog: curated working structures, every one lean.                     #
# --------------------------------------------------------------------------- #

TEMPLATES: tuple[OrgTemplate, ...] = (
    OrgTemplate(
        key="commerce",
        name="Commerce storefront",
        purpose=(
            "Selling things: every order captured whole, stock honest, "
            "customers answered, money accounted. Four seats, one "
            "responsibility each."
        ),
        keywords=(
            "shop", "store", "storefront", "ecommerce", "e-commerce",
            "retail", "sell", "selling", "sales", "orders", "products",
            "merch", "marketplace", "customers",
        ),
        roles=(
            RoleSpec(
                name="Order intake",
                responsibility=(
                    "Answers for every incoming order being captured "
                    "whole and unambiguous before anything else moves."
                ),
                goal=(
                    "Capture an incoming order as the org's canonical "
                    "intake record with a completeness checklist"
                ),
                fields=(
                    "order_id", "customer", "items", "quantities",
                    "destination", "payment_state",
                ),
                checklist=(
                    "Confirm every item and quantity is named",
                    "Confirm the destination is deliverable",
                    "Confirm the payment state before promising anything",
                    "Pass the completed record to fulfilment",
                ),
                authority=2,
            ),
            RoleSpec(
                name="Inventory steward",
                responsibility=(
                    "Answers for the stock record telling the truth — "
                    "what exists, what is reserved, what is gone."
                ),
                goal=(
                    "Keep the canonical stock record: on-hand, reserved, "
                    "and reorder lines with an audit checklist"
                ),
                fields=("sku", "on_hand", "reserved", "reorder_at", "supplier"),
                checklist=(
                    "Reconcile on-hand against the last count",
                    "Reserve stock only against a captured order",
                    "Flag any line at or under its reorder point",
                ),
            ),
            RoleSpec(
                name="Customer reply drafter",
                responsibility=(
                    "Answers for every customer question getting a "
                    "drafted, human-approvable reply — never silence."
                ),
                goal=(
                    "Draft a structured customer reply: the question, "
                    "the facts checked, the proposed answer"
                ),
                fields=("customer", "question", "facts_checked", "draft_reply"),
                checklist=(
                    "Restate the customer's actual question",
                    "Check order and stock facts before answering",
                    "Draft the reply for a human to approve",
                ),
            ),
            RoleSpec(
                name="Finance ledger",
                responsibility=(
                    "Answers for every money movement landing in the "
                    "ledger with its reason attached."
                ),
                goal=(
                    "Record a money movement as a ledger entry with "
                    "amount, direction, reason, and reference"
                ),
                fields=("entry_id", "amount", "direction", "reason", "reference"),
                checklist=(
                    "Attach the order or invoice reference",
                    "State the reason in words",
                    "Balance the day before closing it",
                ),
            ),
        ),
    ),
    OrgTemplate(
        key="software",
        name="Software product studio",
        purpose=(
            "Building software: work triaged once, specified before "
            "built, verified before shipped, and every release explained. "
            "Four seats — coordination stays cheap because each seat owns "
            "its verdict."
        ),
        keywords=(
            "software", "app", "apps", "code", "coding", "development",
            "developer", "engineering", "saas", "platform", "api", "bug",
            "bugs", "release", "product", "startup",
        ),
        roles=(
            RoleSpec(
                name="Intake triage",
                responsibility=(
                    "Answers for every request landing exactly once, "
                    "sized, and pointed at the right seat."
                ),
                goal=(
                    "Triage an incoming request into the org's work "
                    "record: what, for whom, how big, who takes it"
                ),
                fields=("request_id", "summary", "requester", "size", "owner_seat"),
                checklist=(
                    "Restate the request in one sentence",
                    "Reject duplicates by pointing at the existing record",
                    "Size it honestly and route it to one seat",
                ),
                authority=2,
            ),
            RoleSpec(
                name="Spec writer",
                responsibility=(
                    "Answers for work being described precisely enough "
                    "to build without a meeting."
                ),
                goal=(
                    "Write the spec record: behavior, interface, "
                    "acceptance checks — precise enough to build from"
                ),
                fields=("request_id", "behavior", "interface", "acceptance_checks"),
                checklist=(
                    "State the observable behavior, not the implementation",
                    "Name the interface exactly",
                    "Write acceptance checks a machine could verify",
                ),
            ),
            RoleSpec(
                name="Build verifier",
                responsibility=(
                    "Answers for nothing shipping that the acceptance "
                    "checks did not actually pass."
                ),
                goal=(
                    "Verify a build against its spec's acceptance checks "
                    "and record the verdict with evidence"
                ),
                fields=("request_id", "build_ref", "checks_run", "verdict", "evidence"),
                checklist=(
                    "Run every acceptance check, not a sample",
                    "Record the failing output verbatim when it fails",
                    "Refuse the ship, in words, until green",
                ),
            ),
            RoleSpec(
                name="Release scribe",
                responsibility=(
                    "Answers for every release saying what changed and "
                    "for whom, in the user's words."
                ),
                goal=(
                    "Write the release record: version, changes in plain "
                    "words, who is affected, rollback line"
                ),
                fields=("version", "changes", "affected_users", "rollback"),
                checklist=(
                    "Describe each change by its effect, not its diff",
                    "Name who must care",
                    "State the rollback in one line",
                ),
            ),
        ),
    ),
    OrgTemplate(
        key="services",
        name="Client services practice",
        purpose=(
            "Serving clients: every brief captured, every proposal "
            "priced, delivery visible, invoices accounted. Four seats "
            "keep the client's thread in one pair of hands at a time."
        ),
        keywords=(
            "agency", "consulting", "consultancy", "clients", "client",
            "services", "studio", "design", "marketing", "freelance",
            "projects", "creative",
        ),
        roles=(
            RoleSpec(
                name="Client intake",
                responsibility=(
                    "Answers for every client brief being captured whole "
                    "— goal, constraints, deadline — before work starts."
                ),
                goal=(
                    "Capture a client brief as the practice's intake "
                    "record with goal, constraints, and deadline"
                ),
                fields=("client", "goal", "constraints", "deadline", "budget_band"),
                checklist=(
                    "State the client's goal in their own words",
                    "Name every constraint they said and implied",
                    "Confirm the deadline is a date, not a feeling",
                ),
                authority=2,
            ),
            RoleSpec(
                name="Proposal drafter",
                responsibility=(
                    "Answers for every proposal naming scope, price, and "
                    "what is explicitly OUT."
                ),
                goal=(
                    "Draft a proposal record: scope, out-of-scope, "
                    "price, and schedule from an intake brief"
                ),
                fields=("client", "scope", "out_of_scope", "price", "schedule"),
                checklist=(
                    "Write the out-of-scope list first",
                    "Price the scope, not the hope",
                    "Tie every schedule line to a deliverable",
                ),
            ),
            RoleSpec(
                name="Delivery tracker",
                responsibility=(
                    "Answers for the true state of every engagement "
                    "being visible without asking anyone."
                ),
                goal=(
                    "Keep the delivery record: milestones, state, "
                    "blockers, next action per engagement"
                ),
                fields=("engagement", "milestone", "state", "blocker", "next_action"),
                checklist=(
                    "Update state from evidence, not optimism",
                    "Name the blocker and who owns it",
                    "Write the next action as a verb",
                ),
            ),
            RoleSpec(
                name="Invoice ledger",
                responsibility=(
                    "Answers for every deliverable becoming an invoice "
                    "and every invoice being chased to paid."
                ),
                goal=(
                    "Record an invoice: engagement, amount, sent date, "
                    "due date, paid state"
                ),
                fields=("invoice_id", "engagement", "amount", "due", "paid_state"),
                checklist=(
                    "Invoice on delivery, not on memory",
                    "Chase at due date in words",
                    "Reconcile paid against the bank line",
                ),
            ),
        ),
    ),
    OrgTemplate(
        key="government",
        name="Public-service division",
        purpose=(
            "Serving citizens: every case captured, screened against "
            "the rules as written, decided in reviewable words, and "
            "kept on the record. FOUR seats — a division stays this "
            "lean because communication, coordination, trust, and clear "
            "responsibility are what limit it, not headcount."
        ),
        keywords=(
            "government", "ministry", "municipal", "municipality",
            "citizen", "citizens", "civic", "public", "department",
            "permits", "permit", "council", "federal", "administration",
        ),
        roles=(
            RoleSpec(
                name="Case intake",
                responsibility=(
                    "Answers for every citizen case being captured whole, "
                    "acknowledged, and numbered — nobody's request lost."
                ),
                goal=(
                    "Capture a citizen case as the division's numbered "
                    "intake record with an acknowledgement line"
                ),
                fields=("case_no", "citizen", "request", "documents", "received"),
                checklist=(
                    "Number the case before anything else",
                    "List the documents received and the ones missing",
                    "Acknowledge receipt in plain words",
                ),
                authority=2,
            ),
            RoleSpec(
                name="Eligibility screener",
                responsibility=(
                    "Answers for every case being screened against the "
                    "rules AS WRITTEN — the same rules for everyone."
                ),
                goal=(
                    "Screen a case against the written eligibility "
                    "rules and record which rule decided it"
                ),
                fields=("case_no", "rules_applied", "met", "not_met", "screen_verdict"),
                checklist=(
                    "Cite the rule, not the habit",
                    "Record what was met and what was not, separately",
                    "Route edge cases to the decision seat, never guess",
                ),
            ),
            RoleSpec(
                name="Decision drafter",
                responsibility=(
                    "Answers for every decision being drafted in words a "
                    "citizen can read and a review board can check."
                ),
                goal=(
                    "Draft the decision record: verdict, reasons citing "
                    "rules, and the citizen's appeal path"
                ),
                fields=("case_no", "verdict", "reasons", "appeal_path"),
                checklist=(
                    "State the verdict first, then the reasons",
                    "Cite the screening record, rule by rule",
                    "Name the appeal path and its deadline",
                ),
            ),
            RoleSpec(
                name="Records keeper",
                responsibility=(
                    "Answers for the record surviving — complete, "
                    "findable, and retained to the legal schedule."
                ),
                goal=(
                    "File a decided case into the retention record with "
                    "its full chain and retention date"
                ),
                fields=("case_no", "chain", "filed_at", "retain_until"),
                checklist=(
                    "Verify the chain is complete before filing",
                    "Stamp the retention date from the schedule",
                    "Refuse to file a case with gaps, in words",
                ),
            ),
        ),
    ),
    OrgTemplate(
        key="logistics",
        name="Logistics operation",
        purpose=(
            "Moving things: every shipment captured, routed by plan, "
            "exceptions caught while they are cheap, deliveries "
            "accounted. Four seats, no dispatcher's meeting."
        ),
        keywords=(
            "logistics", "shipping", "shipment", "freight", "warehouse",
            "delivery", "deliveries", "fleet", "transport", "courier",
            "supply", "cargo",
        ),
        roles=(
            RoleSpec(
                name="Shipment intake",
                responsibility=(
                    "Answers for every shipment being captured with "
                    "origin, destination, and deadline before it moves."
                ),
                goal=(
                    "Capture a shipment as the operation's intake record "
                    "with origin, destination, contents, deadline"
                ),
                fields=("shipment_id", "origin", "destination", "contents", "deadline"),
                checklist=(
                    "Confirm the destination accepts the contents",
                    "Confirm the deadline is physically possible",
                    "Hand the record to routing, complete",
                ),
                authority=2,
            ),
            RoleSpec(
                name="Route planner",
                responsibility=(
                    "Answers for every shipment having a stated route "
                    "and a stated fallback before departure."
                ),
                goal=(
                    "Plan a shipment's route record: legs, carrier per "
                    "leg, and the fallback leg"
                ),
                fields=("shipment_id", "legs", "carriers", "fallback", "eta"),
                checklist=(
                    "Plan the fallback before committing the route",
                    "Name the carrier for every leg",
                    "State the ETA the customer was told",
                ),
            ),
            RoleSpec(
                name="Exception watcher",
                responsibility=(
                    "Answers for every delay or damage being caught and "
                    "named while fixing it is still cheap."
                ),
                goal=(
                    "Record a shipment exception: what happened, which "
                    "leg, impact on ETA, action taken"
                ),
                fields=("shipment_id", "exception", "leg", "eta_impact", "action"),
                checklist=(
                    "Name the exception in facts, not blame",
                    "Restate the ETA honestly",
                    "Trigger the fallback when the plan says so",
                ),
            ),
            RoleSpec(
                name="Delivery ledger",
                responsibility=(
                    "Answers for every delivery being confirmed, "
                    "reconciled, and closed with its proof."
                ),
                goal=(
                    "Close a delivery into the ledger with proof, "
                    "timestamp, and any variance"
                ),
                fields=("shipment_id", "delivered_at", "proof", "variance"),
                checklist=(
                    "Attach the proof of delivery",
                    "Record variance against the promise",
                    "Close the shipment or say why not",
                ),
            ),
        ),
    ),
    OrgTemplate(
        key="research",
        name="Research group",
        purpose=(
            "Finding things out: questions sharpened before sources are "
            "read, sources digested with citations, findings checked "
            "before they are claimed, reports assembled from checked "
            "findings only."
        ),
        keywords=(
            "research", "lab", "study", "studies", "analysis", "science",
            "scientific", "data", "survey", "academic", "papers",
            "findings", "report", "reports",
        ),
        roles=(
            RoleSpec(
                name="Question intake",
                responsibility=(
                    "Answers for every research question being sharp "
                    "enough that an answer could be recognized."
                ),
                goal=(
                    "Sharpen a research question into the group's intake "
                    "record: question, why, what would count as answered"
                ),
                fields=("question_id", "question", "motivation", "answer_criteria"),
                checklist=(
                    "Rewrite the question until it is falsifiable",
                    "State what evidence would settle it",
                    "Split compound questions before routing",
                ),
                authority=2,
            ),
            RoleSpec(
                name="Source digest",
                responsibility=(
                    "Answers for every source being digested with its "
                    "citation attached — no orphan claims."
                ),
                goal=(
                    "Digest a source into the record: claim, evidence "
                    "type, citation, confidence"
                ),
                fields=("question_id", "source", "claim", "evidence_type", "citation"),
                checklist=(
                    "Quote the claim, then paraphrase it",
                    "Attach the citation before moving on",
                    "Mark secondhand evidence as secondhand",
                ),
            ),
            RoleSpec(
                name="Findings checker",
                responsibility=(
                    "Answers for no finding being claimed that a second "
                    "look could not survive."
                ),
                goal=(
                    "Check a finding against its sources and record the "
                    "verdict: supported, contradicted, or thin"
                ),
                fields=("question_id", "finding", "sources_checked", "verdict"),
                checklist=(
                    "Look for the contradicting source on purpose",
                    "Downgrade thin evidence in words",
                    "Refuse the claim until it survives",
                ),
            ),
            RoleSpec(
                name="Report assembler",
                responsibility=(
                    "Answers for reports containing checked findings "
                    "only, each with its confidence stated."
                ),
                goal=(
                    "Assemble the report record from checked findings: "
                    "answer, confidence, evidence chain, open ends"
                ),
                fields=("question_id", "answer", "confidence", "evidence_chain", "open_ends"),
                checklist=(
                    "Include only verdict-passed findings",
                    "State confidence next to every claim",
                    "List what remains open, honestly",
                ),
            ),
        ),
    ),
    OrgTemplate(
        key="lean-org",
        name="Lean organization",
        purpose=(
            "The general working shape when no specialty fits: intake "
            "that captures, production that does, a check that refuses, "
            "and a ledger that remembers. Four seats run almost "
            "anything — add seats only when a responsibility genuinely "
            "splits."
        ),
        keywords=(),
        roles=(
            RoleSpec(
                name="Intake",
                responsibility=(
                    "Answers for every piece of work being captured "
                    "once, whole, and routed to one owner."
                ),
                goal=(
                    "Capture incoming work as the org's intake record: "
                    "what, for whom, by when, who owns it"
                ),
                fields=("work_id", "summary", "requester", "deadline", "owner"),
                checklist=(
                    "Restate the ask in one sentence",
                    "Refuse duplicates by pointing at the record",
                    "Route to exactly one owner",
                ),
                authority=2,
            ),
            RoleSpec(
                name="Producer",
                responsibility=(
                    "Answers for the work actually getting done and the "
                    "doing being visible in the record."
                ),
                goal=(
                    "Record a unit of production: what was done, from "
                    "which intake, evidence of the result"
                ),
                fields=("work_id", "done", "evidence", "handoff_to"),
                checklist=(
                    "Do the work before writing the record",
                    "Attach evidence, not adjectives",
                    "Hand off to the check, never to done",
                ),
            ),
            RoleSpec(
                name="Quality check",
                responsibility=(
                    "Answers for nothing leaving the org that the check "
                    "did not actually pass."
                ),
                goal=(
                    "Check produced work against its intake and record "
                    "the verdict with the failing detail when it fails"
                ),
                fields=("work_id", "checked_against", "verdict", "failing_detail"),
                checklist=(
                    "Check against the intake, not the producer's memory",
                    "Record the failing detail verbatim",
                    "Refuse in words; never wave through",
                ),
            ),
            RoleSpec(
                name="Ledger",
                responsibility=(
                    "Answers for the org remembering what happened: "
                    "work, verdicts, and money, dated and findable."
                ),
                goal=(
                    "Close a unit of work into the ledger: intake, "
                    "verdict, cost or income, closed date"
                ),
                fields=("work_id", "verdict", "amount", "closed_at"),
                checklist=(
                    "Close only check-passed work",
                    "Attach the money line when there is one",
                    "Date everything",
                ),
            ),
        ),
    ),
)

# The structure imported when nothing matched and no model chose.
FALLBACK_KEY = "lean-org"

# The leanness wall holds at import time — a template that grows past it
# is a bug, not a bigger org.
for _template in TEMPLATES:
    assert len(_template.roles) <= MAX_TEMPLATE_ROLES, _template.key
del _template


def template_by_key(key: str) -> OrgTemplate | None:
    for template in TEMPLATES:
        if template.key == key:
            return template
    return None


_WORD_RE = re.compile(r"[a-z][a-z0-9-]*")


def match_template(
    description: str,
) -> tuple[OrgTemplate | None, tuple[str, ...]]:
    """The deterministic verdict: ``(template, evidence)``.

    Pure keyword arithmetic over the Supernode's description — the same
    words always land on the same template. A winner needs at least
    ``MATCH_EVIDENCE_THRESHOLD`` distinct keyword hits AND a strictly
    better score than the runner-up: a tie is not evidence, it is
    ambiguity, and ambiguity is exactly when the model may be asked."""
    words = set(_WORD_RE.findall((description or "").lower()))
    scored: list[tuple[int, str, OrgTemplate, tuple[str, ...]]] = []
    for template in TEMPLATES:
        evidence = tuple(sorted(kw for kw in template.keywords if kw in words))
        scored.append((len(evidence), template.key, template, evidence))
    # Best score first; key breaks ties deterministically for the report.
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, _, best, best_evidence = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0
    if best_score >= MATCH_EVIDENCE_THRESHOLD and best_score > runner_up:
        return best, best_evidence
    return None, best_evidence if best_score else ()


class ResolvedTemplate(BaseModel):
    model_config = ConfigDict(frozen=True)

    template: OrgTemplate
    # Where the verdict came from: "recorded" (decided once, never
    # re-reasoned), "matched" (deterministic evidence), "model" (thin
    # evidence, the model picked FROM the catalog), or "fallback".
    source: str
    evidence: tuple[str, ...] = ()


# The model's ONLY question when evidence is thin: pick a key. It never
# designs an org chart — the catalog is the plan, reasoning only selects.
CHOOSER_PROMPT = """\
You match an organization to ONE working structure from a fixed catalog.
Read the organization's description and the catalog, then answer with
exactly one catalog key — one word, nothing else. If nothing fits well,
answer: lean-org"""


def model_chooser(model):
    """Wrap a ChatModel as a resolve chooser: one bounded consultation
    that SELECTS a catalog key — never invents a structure. Any answer
    that names no catalog key falls through to the lean fallback."""

    def choose(description: str, catalog) -> str | None:
        lines = "\n".join(
            f"- {key}: {name} — {purpose}" for key, name, purpose in catalog
        )
        raw = model.reply(
            [
                {"role": "system", "content": CHOOSER_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Organization: {description}\n\nCatalog:\n{lines}"
                    ),
                },
            ]
        )
        text = str(raw or "").lower()
        # The key that appears EARLIEST in the answer wins — determinism
        # over a chatty reply that mentions several.
        hits = [
            (text.index(key), key)
            for key, _, _ in catalog
            if key in text
        ]
        return min(hits)[1] if hits else None

    return choose


def resolve_org_template(
    description: str,
    *,
    recorded: str = "",
    chooser=None,
) -> ResolvedTemplate:
    """Resolve a Supernode's description to ONE template, cheapest first.

    1. ``recorded`` — the choice this Supernode already made: returned
       as-is, no matching, no reasoning. Pressing the button twice never
       thinks twice.
    2. Deterministic keyword match with enough evidence.
    3. ``chooser`` — the model hook, consulted ONLY here: called with
       ``(description, catalog)`` where catalog is ``[(key, name,
       purpose), ...]``; must return one catalog key. An unknown or
       empty answer falls through.
    4. The lean-org fallback: a working shape, honestly generic.
    """
    if recorded:
        template = template_by_key(recorded)
        if template is not None:
            return ResolvedTemplate(template=template, source="recorded")
    template, evidence = match_template(description)
    if template is not None:
        return ResolvedTemplate(
            template=template, source="matched", evidence=evidence
        )
    if chooser is not None:
        catalog = [(t.key, t.name, t.purpose) for t in TEMPLATES]
        try:
            picked = str(chooser(description, catalog) or "").strip().lower()
        except Exception:  # noqa: BLE001 - a dead model never blocks the plan
            picked = ""
        chosen = template_by_key(picked)
        if chosen is not None:
            return ResolvedTemplate(
                template=chosen, source="model", evidence=evidence
            )
    fallback = template_by_key(FALLBACK_KEY)
    assert fallback is not None  # the catalog always carries the fallback
    return ResolvedTemplate(
        template=fallback, source="fallback", evidence=evidence
    )
