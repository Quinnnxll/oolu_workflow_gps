from __future__ import annotations


class NodeplaceError(RuntimeError):
    pass


class ContributionError(NodeplaceError):
    pass


class OwnershipError(NodeplaceError):
    pass
