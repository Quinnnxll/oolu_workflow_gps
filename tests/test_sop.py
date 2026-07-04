"""SOP compilation: YAML -> edges, reserved actions, exclusions, validators."""

from __future__ import annotations

from workflow_gps.orchestrator import (
    Blueprint,
    ReservedAction,
    apply_sop_to_blueprint,
)
from workflow_gps.skills import apply_sop_to_skill, parse_sop
from workflow_gps.skills.models import ActionEvent, ReusableSkill, SkillSignature

SOP_YAML = """
sop: month-end-report
applies_to:
  tags: [reporting]
require_order: [export_data, validate_totals, publish]
forbid:
  - operation: "delete_*"
    unless_approved: true
approval:
  operations: [publish]
  approvers: 1
require_verify:
  - operation: publish
    check: workspace.expected_artifacts
risk_budget: 2.0
"""


def _blueprint(*operations: str, name: str = "monthly") -> Blueprint:
    return Blueprint(
        name=name,
        actions=[
            ReservedAction(
                action=ActionEvent(correlation_id="c", adapter="cli", operation=op),
                required_capabilities=frozenset({op}),
                risk="write",
            )
            for op in operations
        ],
    )


def test_parse_sop_yaml():
    sop = parse_sop(SOP_YAML)
    assert sop.name == "month-end-report"
    assert sop.require_order == ["export_data", "validate_totals", "publish"]
    assert sop.forbid[0].operation == "delete_*"
    assert sop.forbid[0].unless_approved is True
    assert sop.approval is not None and sop.approval.operations == ["publish"]
    assert sop.risk_budget == 2.0


def test_scoping_by_tags_and_names():
    sop = parse_sop(SOP_YAML)
    assert sop.matches(name="anything", tags=["reporting"])
    assert not sop.matches(name="anything", tags=["email"])
    unscoped = parse_sop("sop: global\n")
    assert unscoped.matches(name="whatever", tags=[])


def test_require_order_compiles_to_sop_edges():
    sop = parse_sop(SOP_YAML)
    blueprint = _blueprint("export_data", "validate_totals", "publish")
    compiled = apply_sop_to_blueprint(blueprint, sop)
    assert not compiled.excluded
    sop_edges = [e for e in compiled.edges if e.provenance == "sop"]
    assert len(sop_edges) == 2
    ops = {item.action.id: item.action.operation for item in compiled.actions}
    assert [(ops[e.source], ops[e.target]) for e in sop_edges] == [
        ("export_data", "validate_totals"),
        ("validate_totals", "publish"),
    ]


def test_route_missing_a_required_step_is_excluded_not_reordered():
    sop = parse_sop(SOP_YAML)
    compiled = apply_sop_to_blueprint(_blueprint("export_data", "publish"), sop)
    assert compiled.excluded
    assert "validate_totals" in (compiled.exclusion_reason or "")


def test_forbid_excludes_or_reserves():
    hard = parse_sop("sop: strict\nforbid:\n  - operation: 'wipe_*'\n")
    compiled = apply_sop_to_blueprint(_blueprint("wipe_disk"), hard)
    assert compiled.excluded and "forbids" in (compiled.exclusion_reason or "")

    soft = parse_sop(
        "sop: gated\nforbid:\n  - operation: 'wipe_*'\n    unless_approved: true\n"
    )
    compiled = apply_sop_to_blueprint(_blueprint("wipe_disk", "report"), soft)
    assert not compiled.excluded
    by_op = {item.action.operation: item for item in compiled.actions}
    assert by_op["wipe_disk"].reserved is True
    assert by_op["report"].reserved is False


def test_approval_marks_named_operations_reserved():
    sop = parse_sop(SOP_YAML)
    compiled = apply_sop_to_blueprint(
        _blueprint("export_data", "validate_totals", "publish"), sop
    )
    by_op = {item.action.operation: item for item in compiled.actions}
    assert by_op["publish"].reserved is True
    assert by_op["export_data"].reserved is False


def test_risk_budget_forces_approval_on_over_budget_routes():
    sop = parse_sop("sop: tight\nrisk_budget: 0.5\n")
    # Two write actions at 0.5 each = 1.0 > 0.5: every non-read action reserved.
    compiled = apply_sop_to_blueprint(_blueprint("write_a", "write_b"), sop)
    assert all(item.reserved for item in compiled.actions)

    roomy = parse_sop("sop: roomy\nrisk_budget: 5.0\n")
    compiled = apply_sop_to_blueprint(_blueprint("write_a", "write_b"), roomy)
    assert not any(item.reserved for item in compiled.actions)


def test_require_verify_appends_hard_validator_to_skill():
    sop = parse_sop(SOP_YAML)
    skill = ReusableSkill(
        name="monthly",
        description="",
        signature=SkillSignature(application="cli", adapter="cli"),
        actions=[ActionEvent(correlation_id="c", adapter="cli", operation="publish")],
    )
    updated = apply_sop_to_skill(skill, sop)
    assert len(updated.validators) == 1
    assert updated.validators[0].validator == "workspace.expected_artifacts"
    # Idempotent: applying twice does not duplicate.
    assert len(apply_sop_to_skill(updated, sop).validators) == 1
    # An SOP whose verify targets a different operation adds nothing.
    other = parse_sop(
        "sop: other\nrequire_verify:\n  - operation: send_email\n    check: x\n"
    )
    assert apply_sop_to_skill(skill, other).validators == []
