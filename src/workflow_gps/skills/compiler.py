"""Conservative demonstration compiler: exact first, generalization later."""

from __future__ import annotations

from .models import (
    ConstraintSeverity,
    ConstraintSpec,
    ConstraintStatus,
    Demonstration,
    ReusableSkill,
    SkillSignature,
)
from .workspace import changed_artifacts


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
