"""Level B: a subsystem change, end to end, under an identical budget.

The industrial vertical's benchmark (step 6; spec §22): a CHANGE
arrives — the suspension shaft grows from 8 mm to 12 mm — and a planner
must propagate it through everything the vertical built:

    read the graph          (what is true now)
    propose to the kernel   (change the bracket's bore, honestly)
    run the CAD hand        (REAL geometry: rebuild, measure, intersect)
    file the evidence       (append the measurements to the graph)
    advance the status      (draft -> approved, past walls and critics)

Every contender gets the SAME tools, the SAME world, and the SAME
counted budget — reads are free, writes and executions are counted.
The report scores the spec's metrics (completion, caught violations,
demoted runs, verified progress per counted step) and the §23 gate
decides fitness for the seat. A model-backed planner auditions by
implementing one function:

    def my_planner(bench: Bench) -> None   # drive until done or broke

Run it:  python benchmarks/level_b.py
The same claims run in CI as tests/test_level_b.py.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from oolu.durable import DurableConnection, FilesystemArtifactStore
from oolu.orchestrator import ActionExecutorRouteRunner
from oolu.orchestrator.state import Blueprint, ReservedAction, RoutePlan
from oolu.projectgraph import (
    ConstraintSpec,
    GraphObject,
    GraphProposal,
    PatchOp,
    ProjectGraphStore,
    TransactionKernel,
)
from oolu.skills.cad_adapter import CadActionExecutor
from oolu.skills.models import ActionEvent, ExecutionStatus, Postcondition

PROJECT = "veh-bench"
OWNER = "lead"
TENANT = "t1"
ALUMINIUM = 2700.0  # kg/m^3

# The change event and the walls around the response.
SHAFT_D_BEFORE = 8.0
SHAFT_D_AFTER = 12.0
RADIAL_CLEARANCE_MM = 1.0  # bore must beat the shaft by 2mm on diameter
MAX_BORE_MM = 20.0  # the manufacturability wall (hard constraint)
MASS_BUDGET_KG = 0.035

DEFAULT_BUDGET = 12  # counted steps: proposals + CAD runs; reads are free


class BudgetExhausted(RuntimeError):
    """The identical-budget wall: the planner spent its counted steps."""


@dataclass
class Report:
    """One contender's audition, in the spec's vocabulary."""

    name: str
    budget: int
    steps_used: int = 0
    proposals_committed: int = 0
    proposals_rejected: int = 0
    cad_runs_ok: int = 0
    cad_runs_demoted: int = 0
    budget_exhausted: bool = False
    acceptance: dict[str, bool] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return bool(self.acceptance) and all(self.acceptance.values())

    @property
    def progress_per_step(self) -> float:
        """The primary §22 metric: verified progress over spend."""
        if not self.steps_used:
            return 0.0
        return sum(self.acceptance.values()) / self.steps_used


def fit_for_the_seat(report: Report) -> bool:
    """The §23 domain-lead gate, compact: the milestone completes within
    budget, and every hard-constraint violation was CAUGHT by the kernel
    (rejections are reported by construction — what disqualifies is not
    finishing)."""
    return report.completed and not report.budget_exhausted


class Bench:
    """The identical tools every contender gets. Reads are free;
    proposals and CAD runs spend the budget."""

    def __init__(self, workdir: Path, *, budget: int) -> None:
        self._conn = DurableConnection(workdir / "bench.db")
        self.store = ProjectGraphStore(self._conn)
        self.kernel = TransactionKernel(self.store)
        self._runner = ActionExecutorRouteRunner(
            {
                "cad": CadActionExecutor(
                    artifacts=FilesystemArtifactStore(workdir / "blobs")
                )
            }
        )
        self.report: Report | None = None  # attached by run()
        self._budget = budget
        self._keys = 0

    def close(self) -> None:
        self._conn.close()

    # -- free reads ----------------------------------------------------- #
    def read(self, object_id: str) -> GraphObject | None:
        return self.store.get(PROJECT, object_id)

    # -- counted verbs ---------------------------------------------------#
    def _spend(self) -> None:
        assert self.report is not None
        if self.report.steps_used >= self._budget:
            self.report.budget_exhausted = True
            raise BudgetExhausted("the counted budget is spent")
        self.report.steps_used += 1

    def propose(self, reason: str, patch: list[PatchOp]):
        self._spend()
        result = self.kernel.process(
            GraphProposal(
                project_id=PROJECT, owner=OWNER, reason=reason, patch=patch
            ),
            tenant=TENANT,
        )
        assert self.report is not None
        if result.status == "committed":
            self.report.proposals_committed += 1
        else:
            self.report.proposals_rejected += 1
        return result

    def run_cad(self, action: ActionEvent):
        """One judged CAD execution: the evaluator demotes API successes
        that break the action's declared postconditions."""
        self._spend()
        self._keys += 1
        record = self._runner.execute(
            RoutePlan(
                chosen=Blueprint(
                    name="bench-cad", actions=[ReservedAction(action=action)]
                ),
                alternatives=[],
            ),
            idempotency_key=f"bench-{self._keys}",
            attempt=1,
        )
        outcome = record.action_outcomes[-1]
        assert self.report is not None
        if outcome.status is ExecutionStatus.SUCCEEDED:
            self.report.cad_runs_ok += 1
        else:
            self.report.cad_runs_demoted += 1
        return outcome


# --------------------------------------------------------------------------- #
# The world: two components, one wall, one incoming change.                    #
# --------------------------------------------------------------------------- #
def bracket_features(parameters: dict) -> list[dict]:
    return [
        {
            "kind": "box",
            "x_mm": parameters["x_mm"],
            "y_mm": parameters["y_mm"],
            "z_mm": parameters["z_mm"],
        },
        {"kind": "hole", "d_mm": parameters["bore_d_mm"]},
    ]


def shaft_features(parameters: dict) -> list[dict]:
    return [
        {"kind": "cylinder", "d_mm": parameters["d_mm"], "h_mm": parameters["h_mm"]}
    ]


def seed_world(bench: Bench) -> None:
    """The truth before the change, plus the change event itself —
    committed through the kernel like everything else."""
    bench.store.ensure_project(PROJECT, tenant=TENANT, owner=OWNER)
    seed = GraphProposal(
        project_id=PROJECT,
        owner=OWNER,
        reason="seed the suspension subsystem",
        patch=[
            PatchOp(
                op="create",
                object=GraphObject(
                    object_id="shaft-1",
                    path="subsystems/suspension/shaft",
                    type="component",
                    parameters={"d_mm": SHAFT_D_BEFORE, "h_mm": 40.0},
                ),
            ),
            PatchOp(
                op="create",
                object=GraphObject(
                    object_id="bracket-1",
                    path="subsystems/suspension/bracket",
                    type="component",
                    parameters={
                        "x_mm": 40.0,
                        "y_mm": 30.0,
                        "z_mm": 10.0,
                        "bore_d_mm": 10.0,
                    },
                    constraints=[
                        ConstraintSpec(
                            name="manufacturable-bore",
                            severity="hard",
                            pointer="parameters/bore_d_mm",
                            op="<=",
                            value=MAX_BORE_MM,
                        )
                    ],
                ),
            ),
        ],
    )
    assert bench.kernel.process(seed, tenant=TENANT).status == "committed"
    change = GraphProposal(
        project_id=PROJECT,
        owner=OWNER,
        reason="requirement change: the shaft grows to 12mm",
        patch=[
            PatchOp(
                op="set",
                object_id="shaft-1",
                base_revision=1,
                pointer="parameters/d_mm",
                old_value=SHAFT_D_BEFORE,
                new_value=SHAFT_D_AFTER,
            )
        ],
    )
    assert bench.kernel.process(change, tenant=TENANT).status == "committed"


def acceptance(bench: Bench) -> dict[str, bool]:
    """The deterministic finish line, recomputed from the graph alone.

    Evidence must cite the CURRENT shaft — measurements of yesterday's
    world verify nothing."""
    shaft = bench.read("shaft-1")
    bracket = bench.read("bracket-1")
    if shaft is None or bracket is None:
        return {"world": False}
    bore = float(bracket.parameters.get("bore_d_mm", 0))
    shaft_d = float(shaft.parameters.get("d_mm", 0))
    checks = {
        "clearance": bore >= shaft_d + 2 * RADIAL_CLEARANCE_MM,
        "manufacturable": bore <= MAX_BORE_MM,
        "approved": bracket.status == "approved",
    }
    current = [
        entry
        for entry in bracket.evidence
        if entry.get("shaft_d_mm") == shaft_d
        and entry.get("bore_d_mm") == bore
    ]
    checks["interference_verified"] = any(
        entry.get("interference_count") == 0 for entry in current
    )
    checks["mass_verified"] = any(
        isinstance(entry.get("mass_kg"), (int, float))
        and entry["mass_kg"] <= MASS_BUDGET_KG
        for entry in current
    )
    return checks


def run(
    planner: Callable[[Bench], None], *, name: str, budget: int = DEFAULT_BUDGET
) -> Report:
    """One audition: same world, same tools, same budget — then the
    finish line is recomputed from the graph, never from the planner's
    own account of itself."""
    with tempfile.TemporaryDirectory() as workdir:
        bench = Bench(Path(workdir), budget=budget)
        try:
            bench.report = Report(name=name, budget=budget)
            seed_world(bench)
            try:
                planner(bench)
            except BudgetExhausted:
                pass
            bench.report.acceptance = acceptance(bench)
            return bench.report
        finally:
            bench.close()


# --------------------------------------------------------------------------- #
# Contenders.                                                                  #
# --------------------------------------------------------------------------- #
def careful_engineer(bench: Bench) -> None:
    """The scripted baseline: read, change honestly, REBUILD AND MEASURE,
    file the evidence, then advance. Every future model-backed planner
    must beat or match this within the same budget."""
    shaft = bench.read("shaft-1")
    bracket = bench.read("bracket-1")
    assert shaft is not None and bracket is not None
    target_bore = float(shaft.parameters["d_mm"]) + 2 * RADIAL_CLEARANCE_MM

    changed = bench.propose(
        "grow the bore for the new shaft, with radial clearance",
        [
            PatchOp(
                op="set",
                object_id="bracket-1",
                base_revision=bracket.revision,
                pointer="parameters/bore_d_mm",
                old_value=bracket.parameters["bore_d_mm"],
                new_value=target_bore,
            )
        ],
    )
    assert changed.status == "committed", changed.reasons
    bracket = bench.read("bracket-1")
    assert bracket is not None

    build = bench.run_cad(
        ActionEvent(
            correlation_id="bench",
            adapter="cad",
            operation="build",
            parameters={
                "features": bracket_features(bracket.parameters),
                "density_kg_m3": ALUMINIUM,
                "name": "bracket",
            },
            postconditions=[
                Postcondition(
                    name="mass-budget",
                    pointer="mass_kg",
                    op="<=",
                    value=MASS_BUDGET_KG,
                ),
                Postcondition(
                    name="solid", pointer="solid_ok", op="==", value=True
                ),
            ],
        )
    )
    fit = bench.run_cad(
        ActionEvent(
            correlation_id="bench",
            adapter="cad",
            operation="assemble",
            parameters={
                "parts": [
                    {
                        "name": "bracket",
                        "features": bracket_features(bracket.parameters),
                        "position": [0, 0, 0],
                    },
                    {
                        "name": "shaft",
                        "features": shaft_features(shaft.parameters),
                        "position": [0, 0, 0],
                    },
                ]
            },
            postconditions=[
                Postcondition(
                    name="no-interference",
                    pointer="interference_count",
                    op="==",
                    value=0,
                )
            ],
        )
    )
    if (
        build.status is not ExecutionStatus.SUCCEEDED
        or fit.status is not ExecutionStatus.SUCCEEDED
    ):
        return  # the honest engineer never files or advances unverified work

    filed = bench.propose(
        "file the verified measurements against the current shaft",
        [
            PatchOp(
                op="append",
                object_id="bracket-1",
                base_revision=bracket.revision,
                pointer="evidence",
                new_value={
                    "kind": "cad-verification",
                    "shaft_d_mm": shaft.parameters["d_mm"],
                    "bore_d_mm": bracket.parameters["bore_d_mm"],
                    "mass_kg": build.evidence["mass_kg"],
                    "interference_count": fit.evidence["interference_count"],
                },
            )
        ],
    )
    assert filed.status == "committed", filed.reasons
    bracket = bench.read("bracket-1")
    assert bracket is not None
    bench.propose(
        "verified against the grown shaft — advance",
        [
            PatchOp(
                op="set",
                object_id="bracket-1",
                base_revision=bracket.revision,
                pointer="status",
                old_value=bracket.status,
                new_value="approved",
            )
        ],
    )


def reckless_intern(bench: Bench) -> None:
    """The pretender: bores past the manufacturability wall (caught by
    the kernel), never re-measures, and approves anyway. The gate must
    say no."""
    bracket = bench.read("bracket-1")
    assert bracket is not None
    bench.propose(
        "just make the hole huge to be safe",
        [
            PatchOp(
                op="set",
                object_id="bracket-1",
                base_revision=bracket.revision,
                pointer="parameters/bore_d_mm",
                old_value=bracket.parameters["bore_d_mm"],
                new_value=30.0,  # past the 20mm wall: the kernel refuses
            )
        ],
    )
    bench.propose(
        "ship it",
        [
            PatchOp(
                op="set",
                object_id="bracket-1",
                base_revision=bracket.revision,
                pointer="status",
                old_value=bracket.status,
                new_value="approved",
            )
        ],
    )


CONTENDERS: dict[str, Callable[[Bench], None]] = {
    "careful-engineer": careful_engineer,
    "reckless-intern": reckless_intern,
}


def main() -> None:
    print(
        f"Level B: shaft {SHAFT_D_BEFORE:.0f}mm -> {SHAFT_D_AFTER:.0f}mm; "
        f"budget {DEFAULT_BUDGET} counted steps\n"
    )
    header = (
        f"{'contender':<18} {'steps':>5} {'ok/rej':>7} {'cad ok/dem':>10} "
        f"{'done':>5} {'prog/step':>9}  gate"
    )
    print(header)
    for name, planner in CONTENDERS.items():
        report = run(planner, name=name)
        verdict = "FIT" if fit_for_the_seat(report) else "not fit"
        print(
            f"{report.name:<18} {report.steps_used:>5} "
            f"{report.proposals_committed}/{report.proposals_rejected:>3} "
            f"{report.cad_runs_ok:>6}/{report.cad_runs_demoted:<3} "
            f"{str(report.completed):>5} {report.progress_per_step:>9.3f}  "
            f"{verdict}"
        )
        for check, passed in report.acceptance.items():
            print(f"{'':<18}   {'✓' if passed else '✗'} {check}")


if __name__ == "__main__":
    main()
