"""The transactional kernel — the ONLY door through which truth changes.

Models propose; this kernel commits. Every proposal walks the same
gauntlet, in order, and dies at the first wall with the reason in
words:

1. SHAPE — a reason is required, the patch is non-empty, every op is
   well-formed for its kind.
2. TERRITORY — the submitter is the project's owner, or holds granted
   write scope over every touched path; ``forbidden`` always wins. A
   principal with no grant can change NOTHING — fail closed.
3. TIME — every op declares the revision it was reasoned against, and a
   ``set`` declares the exact old value it believes it is replacing.
   Stale proposals are rejected, never merged: the model rebases and
   re-reasons, the kernel does not guess.
4. WALLS — hard constraints that PASSED at the base revision must still
   pass on the candidate (protected regressions are rejected). A
   pre-existing violation may persist (it is an open issue, not a new
   sin) but is said out loud as a warning; soft failures warn too. New
   objects must pass all their hard constraints outright.
5. COMMIT — all objects of the proposal land in one transaction, each
   revision bumped exactly once, history kept forever, and the decision
   (either way) is appended to the hash-chained audit log.

Rollback is trivial by construction: a rejected proposal never touched
the graph, and any committed revision can be reverted by a NEW proposal
that says why — truth only ever moves forward, on the record.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable

from ..predicates import pointer_root, resolve_pointer
from .models import (
    OBJECT_STATUSES,
    GraphObject,
    GraphProposal,
    ProposalResult,
    evaluate_constraint,
    path_covered,
    valid_set_pointer,
)
from .store import ProjectGraphStore


class TransactionKernel:
    def __init__(
        self,
        store: ProjectGraphStore,
        *,
        audit: Callable[[str, dict], Any] | None = None,
    ) -> None:
        self._store = store
        self._audit = audit

    def process(
        self, proposal: GraphProposal, *, tenant: str
    ) -> ProposalResult:
        """One proposal, one verdict. ``proposal.owner`` is the
        SUBMITTING principal (stamped by the door from the session,
        never taken from the body)."""
        reasons: list[str] = []
        warnings: list[str] = []
        project = self._store.project(proposal.project_id, tenant=tenant)
        if project is None:
            return self._reject(proposal, ["no such project"])

        self._validate_shape(proposal, reasons)
        if reasons:
            return self._reject(proposal, reasons)

        self._validate_territory(proposal, project, reasons)
        if reasons:
            return self._reject(proposal, reasons)

        candidates = self._build_candidates(proposal, reasons)
        if reasons or candidates is None:
            return self._reject(proposal, reasons or ["nothing to apply"])

        self._protect_constraints(candidates, reasons, warnings)
        if reasons:
            return self._reject(proposal, reasons, warnings)

        self._gate_advancement(proposal.project_id, candidates, reasons)
        if reasons:
            return self._reject(proposal, reasons, warnings)

        committed = [candidate for _base, candidate in candidates.values()]
        self._store.apply_commit(
            proposal.project_id, committed, proposal_id=proposal.proposal_id
        )
        result = ProposalResult(
            proposal_id=proposal.proposal_id,
            status="committed",
            warnings=warnings,
            revisions={c.object_id: c.revision for c in committed},
        )
        self._store.record_proposal(proposal, result)
        if self._audit is not None:
            self._audit(
                "graph.committed",
                {
                    "project_id": proposal.project_id,
                    "proposal_id": proposal.proposal_id,
                    "by": proposal.owner,
                    "node_id": proposal.node_id,
                    "reason": proposal.reason,
                    "revisions": result.revisions,
                },
            )
        return result

    # ------------------------------------------------------------------ #
    def _reject(
        self,
        proposal: GraphProposal,
        reasons: list[str],
        warnings: list[str] | None = None,
    ) -> ProposalResult:
        result = ProposalResult(
            proposal_id=proposal.proposal_id,
            status="rejected",
            reasons=reasons,
            warnings=warnings or [],
        )
        self._store.record_proposal(proposal, result)
        if self._audit is not None:
            self._audit(
                "graph.rejected",
                {
                    "project_id": proposal.project_id,
                    "proposal_id": proposal.proposal_id,
                    "by": proposal.owner,
                    "reasons": reasons,
                },
            )
        return result

    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_shape(proposal: GraphProposal, reasons: list[str]) -> None:
        if not proposal.reason.strip():
            reasons.append("every change includes a reason — this one has none")
        if not proposal.patch:
            reasons.append("an empty patch changes nothing")
        for index, op in enumerate(proposal.patch):
            where = f"op {index + 1}"
            if op.op == "create":
                if op.object is None:
                    reasons.append(f"{where}: create carries no object")
                elif not op.object.path.strip("/"):
                    reasons.append(f"{where}: an object needs a path")
                elif op.object.status not in OBJECT_STATUSES:
                    reasons.append(
                        f"{where}: unknown status '{op.object.status}'"
                    )
                continue
            if not op.object_id:
                reasons.append(f"{where}: which object? none named")
            if op.base_revision is None:
                reasons.append(
                    f"{where}: no base revision — a proposal must declare "
                    "the truth it reasoned against"
                )
            if op.op == "append" and op.pointer.strip("/") not in (
                "evidence",
                "relations",
            ):
                reasons.append(
                    f"{where}: append reaches only evidence or relations"
                )
            if op.op == "set" and not valid_set_pointer(op.pointer):
                reasons.append(
                    f"{where}: '{op.pointer}' is not a settable pointer "
                    "(parameters/…, native_references/…, relations, "
                    "evidence, or status)"
                )

    def _validate_territory(
        self, proposal: GraphProposal, project: dict, reasons: list[str]
    ) -> None:
        principal = proposal.owner
        if principal == project["owner"]:
            return  # the project's owner answers for all of it
        scopes = self._store.scopes_for(proposal.project_id, principal)
        for op in proposal.patch:
            path = (
                op.object.path
                if op.op == "create" and op.object is not None
                else self._current_path(proposal.project_id, op.object_id)
            )
            if path is None:
                continue  # revision validation will name the missing object
            if scopes is None:
                reasons.append(
                    f"'{principal}' holds no territory in this project — "
                    "the owner grants write scopes before anyone changes "
                    "anything"
                )
                return
            if path_covered(path, scopes.forbidden_paths):
                reasons.append(f"'{path}' is forbidden territory for "
                               f"'{principal}'")
            elif not path_covered(path, scopes.write_paths):
                reasons.append(
                    f"'{path}' is outside '{principal}'s granted write paths"
                )

    def _current_path(self, project_id: str, object_id: str) -> str | None:
        current = self._store.get(project_id, object_id)
        return current.path if current is not None else None

    # ------------------------------------------------------------------ #
    def _build_candidates(
        self, proposal: GraphProposal, reasons: list[str]
    ) -> dict[str, tuple[GraphObject | None, GraphObject]] | None:
        """object_id -> (base or None, candidate), applying every op in
        order. Ops may stack on the same object within one proposal —
        later ops see earlier ones' work, and the revision bumps ONCE."""
        candidates: dict[str, tuple[GraphObject | None, GraphObject]] = {}
        now = datetime.now(UTC)
        for index, op in enumerate(proposal.patch):
            where = f"op {index + 1}"
            if op.op == "create":
                assert op.object is not None  # shape-validated above
                obj = op.object
                if (
                    obj.object_id in candidates
                    or self._store.get(proposal.project_id, obj.object_id)
                    is not None
                ):
                    reasons.append(
                        f"{where}: object '{obj.object_id}' already exists — "
                        "truth is changed by set, never re-created"
                    )
                    continue
                candidate = obj.model_copy(
                    update={
                        "project_id": proposal.project_id,
                        "revision": 1,
                        "owner": obj.owner or proposal.owner,
                        "updated_at": now,
                    }
                )
                candidates[candidate.object_id] = (None, candidate)
                continue

            base, working = candidates.get(op.object_id, (None, None))
            if working is None:
                base = self._store.get(proposal.project_id, op.object_id)
                if base is None:
                    reasons.append(f"{where}: no object '{op.object_id}'")
                    continue
                working = base.model_copy(
                    update={"revision": base.revision + 1, "updated_at": now}
                )
            if base is not None and op.base_revision != base.revision:
                reasons.append(
                    f"{where}: stale — '{op.object_id}' is at revision "
                    f"{base.revision}, the proposal reasoned against "
                    f"{op.base_revision}; rebase and re-reason"
                )
                continue

            if op.op == "supersede":
                working = working.model_copy(update={"status": "superseded"})
            elif op.op == "append":
                field = op.pointer.strip("/")
                grown = [*getattr(working, field), op.new_value]
                working = working.model_copy(update={field: grown})
            else:
                updated = self._apply_set(working, op, where, reasons)
                if updated is None:
                    continue
                working = updated
            candidates[op.object_id] = (base, working)
        return candidates if not reasons else None

    @staticmethod
    def _apply_set(
        working: GraphObject, op, where: str, reasons: list[str]
    ) -> GraphObject | None:
        pointer = op.pointer.strip("/")
        if pointer == "status":
            if op.new_value not in OBJECT_STATUSES:
                reasons.append(f"{where}: unknown status '{op.new_value}'")
                return None
            if working.status != op.old_value:
                reasons.append(
                    f"{where}: status is '{working.status}', not the "
                    f"'{op.old_value}' the proposal expected"
                )
                return None
            return working.model_copy(update={"status": op.new_value})
        if pointer in ("relations", "evidence"):
            current = getattr(working, pointer)
            if current != (op.old_value if op.old_value is not None else []):
                reasons.append(
                    f"{where}: {pointer} changed since the proposal read it"
                )
                return None
            return working.model_copy(update={pointer: op.new_value})
        # Nested: parameters/… or native_references/…
        root = pointer_root(pointer)
        payload = getattr(working, root)
        exists, current = resolve_pointer({root: payload}, pointer)
        if current != op.old_value or (op.old_value is None and exists):
            held = current if exists else "nothing"
            reasons.append(
                f"{where}: '{pointer}' holds {held!r}, not the "
                f"{op.old_value!r} the proposal expected"
            )
            return None
        # Rebuild the nested dict with the one leaf changed.
        parts = pointer.split("/")[1:]
        fresh = dict(payload)
        cursor = fresh
        for part in parts[:-1]:
            child = cursor.get(part)
            child = dict(child) if isinstance(child, dict) else {}
            cursor[part] = child
            cursor = child
        cursor[parts[-1]] = op.new_value
        return working.model_copy(update={root: fresh})

    # ------------------------------------------------------------------ #
    # Critics have teeth: an OPEN blocking finding stops the climb.        #
    # ------------------------------------------------------------------ #
    _ADVANCED = ("approved", "released")

    def _gate_advancement(
        self,
        project_id: str,
        candidates: dict[str, tuple[GraphObject | None, GraphObject]],
        reasons: list[str],
    ) -> None:
        """Model output is not project truth until it climbs the ladder —
        and an object with an OPEN blocking finding does not climb.
        Fixing parameters stays allowed (that is HOW findings get
        resolved); only advancement to approved/released is gated."""
        for base, candidate in candidates.values():
            if candidate.type == "finding":
                continue
            advancing = candidate.status in self._ADVANCED and (
                base is None or base.status != candidate.status
            )
            if not advancing:
                continue
            open_blocking = [
                obj
                for obj in self._store.list(project_id, path="issues")
                if obj.type == "finding"
                and obj.parameters.get("target") == candidate.object_id
                and obj.parameters.get("state") == "open"
                and obj.parameters.get("severity") == "blocking"
            ]
            for finding in open_blocking:
                reasons.append(
                    f"'{candidate.object_id}' cannot advance to "
                    f"'{candidate.status}': open blocking finding "
                    f"'{finding.object_id}' — "
                    f"{finding.parameters.get('finding', '')} "
                    f"(recommended: "
                    f"{finding.parameters.get('recommended_action', '')})"
                )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _protect_constraints(
        candidates: dict[str, tuple[GraphObject | None, GraphObject]],
        reasons: list[str],
        warnings: list[str],
    ) -> None:
        for base, candidate in candidates.values():
            base_payload = base.model_dump(mode="json") if base else None
            payload = candidate.model_dump(mode="json")
            for constraint in candidate.constraints:
                ok = evaluate_constraint(payload, constraint)
                if ok:
                    continue
                if constraint.severity == "soft":
                    warnings.append(
                        f"soft constraint '{constraint.name}' fails on "
                        f"'{candidate.object_id}'"
                    )
                    continue
                passed_before = base_payload is not None and evaluate_constraint(
                    base_payload, constraint
                )
                if passed_before:
                    reasons.append(
                        f"hard constraint '{constraint.name}' would REGRESS "
                        f"on '{candidate.object_id}' — previously passed "
                        "walls are protected"
                    )
                elif base is None:
                    reasons.append(
                        f"hard constraint '{constraint.name}' fails on new "
                        f"object '{candidate.object_id}'"
                    )
                else:
                    warnings.append(
                        f"hard constraint '{constraint.name}' on "
                        f"'{candidate.object_id}' was already failing — the "
                        "open violation persists, unresolved"
                    )
