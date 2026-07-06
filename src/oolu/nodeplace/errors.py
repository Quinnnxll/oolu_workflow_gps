from __future__ import annotations


class NodeplaceError(RuntimeError):
    pass


class ContributionError(NodeplaceError):
    pass


class OwnershipError(NodeplaceError):
    pass


class SafetyViolation(ContributionError):
    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("; ".join(violations))


class RatingError(NodeplaceError):
    pass


class UnverifiedRunError(RatingError):
    pass
