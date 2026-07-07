"""Node hygiene: clones, fraud, and zombies — agreed away upfront.

Creating a node means agreeing to the NODE POLICY before anything is
published: the platform DETECTS misbehaving nodes deterministically and
RESTRICTS or REMOVES them. Nobody is surprised later, because the power
being exercised is the one the creator signed up to on day one.

Three findings, three detectors, no model in the loop:

- **clone** — a node whose version carries the same content hash as a
  version another node published EARLIER. Copying a listing to farm its
  traffic is removal-grade: the clone is revoked (delisted) and its
  account restricted; the original is never touched.
- **fraud** — a node with real, platform-verified failures and not one
  verified success: it keeps selling work that never works. Restricted
  until its responsible fixes it and re-verifies.
- **zombie** — a node past the abandonment window with no activity ever:
  no run was ever bound to any of its versions since publication.
  Restricted, not removed — a returning responsible can revive it.

Restriction is REAL, not a label: a restricted node refuses new contract
runs (the gateway wall) and disappears from marketplace ranking (the
assembler skips it). The platform's own built-ins (the ``oolu.``
namespace) are exempt from the zombie rule — a prebuilt hand with no
traffic yet is not somebody's abandoned stall.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict

from ..skills.models import ReusableSkill
from .accounts import NodeStatus

NODE_POLICY_VERSION = "2026.07"

NODE_POLICY = (
    "The Node Policy, agreed at creation: (1) no clones — publishing a "
    "copy of another node's content is removal-grade; (2) no fraud — a "
    "node whose verified record is failures without a single success is "
    "restricted until fixed; (3) no zombies — a node abandoned past the "
    "activity window is restricted until its responsible returns. The "
    "platform detects all three deterministically and restricts or "
    "removes offenders; a restricted node refuses new runs and leaves "
    "the marketplace ranking."
)

# The platform's own prebuilt namespace — never somebody's abandoned stall.
_PLATFORM_NAMESPACE = "oolu."


def content_fingerprint(sanitized_skill_json: str) -> str | None:
    """A canonical hash of what a version actually IS — name, wording,
    typed parameters, and the executable actions — with every volatile
    field (ids, timestamps) left out. Two versions with the same
    fingerprint carry the same content, whoever published them."""
    try:
        skill = ReusableSkill.model_validate_json(sanitized_skill_json)
    except ValueError:
        return None
    canon = {
        "name": skill.name.strip().lower(),
        "description": skill.description.strip().lower(),
        "signature": [skill.signature.application, skill.signature.adapter],
        "parameters": sorted([p.name, p.value_type] for p in skill.parameters),
        "actions": [
            [
                a.adapter,
                a.operation,
                json.dumps(a.parameters, sort_keys=True, default=str),
            ]
            for a in skill.actions
        ],
    }
    return hashlib.sha256(
        json.dumps(canon, sort_keys=True).encode("utf-8")
    ).hexdigest()


class HygieneKind(str, Enum):
    CLONE = "clone"
    FRAUD = "fraud"
    ZOMBIE = "zombie"


class HygieneFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    node_id: str
    kind: HygieneKind
    evidence: str
    # What the sweep did: "revoked" (removal) or "restricted"; inspect-only
    # findings carry the action the sweep WOULD take.
    action: str


class NodeHygieneService:
    """Detect clone / fraud / zombie nodes; restrict or remove them."""

    def __init__(
        self,
        *,
        registry,  # nodeplace.RegistryStore
        accounts,  # nodeplace.NodeAccountStore
        stats=None,  # economics.StatsSource: platform-verified outcomes
        attribution=None,  # metering.AttributionStore: bound runs
        fraud_min_failures: int = 3,
        zombie_after_days: int = 90,
        clock=None,
    ) -> None:
        self._registry = registry
        self._accounts = accounts
        self._stats = stats
        self._attribution = attribution
        self._fraud_min_failures = fraud_min_failures
        self._zombie_after_days = zombie_after_days
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------ #
    def is_restricted(self, node_id: str) -> bool:
        account = self._accounts.get(node_id)
        return account is not None and account.status is NodeStatus.RESTRICTED

    def restricted_versions(self, version_ids: list[str]) -> set[str]:
        """Which of these versions belong to restricted (or revoked)
        nodes — the run path refuses to bind them."""
        blocked: set[str] = set()
        for version_id in version_ids:
            version = self._registry.get_version(version_id)
            if version is None:
                continue
            node = self._registry.get_node(version.node_id)
            if node is not None and node.revoked_at is not None:
                blocked.add(version_id)
                continue
            if self.is_restricted(version.node_id):
                blocked.add(version_id)
        return blocked

    # ------------------------------------------------------------------ #
    def inspect(self) -> list[HygieneFinding]:
        """Detect only — what the sweep would do, without doing it."""
        findings: list[HygieneFinding] = []
        nodes = [n for n in self._registry.all_nodes() if n.revoked_at is None]
        by_id = {n.node_id: n for n in nodes}
        versions = {
            n.node_id: self._registry.list_versions(n.node_id) for n in nodes
        }

        # Clones: the same canonical content, published later by another
        # node (volatile ids/timestamps excluded — see content_fingerprint).
        marks: dict[str, str | None] = {
            v.version_id: content_fingerprint(v.sanitized_skill_json)
            for vs in versions.values()
            for v in vs
        }
        earliest: dict[str, tuple[str, datetime]] = {}
        for node_id, vs in versions.items():
            for v in vs:
                mark = marks[v.version_id]
                if mark is None:
                    continue
                seen = earliest.get(mark)
                if seen is None or v.published_at < seen[1]:
                    earliest[mark] = (node_id, v.published_at)
        for node_id, vs in versions.items():
            for v in vs:
                mark = marks[v.version_id]
                if mark is None:
                    continue
                original_id, _at = earliest[mark]
                if original_id != node_id:
                    findings.append(
                        HygieneFinding(
                            node_id=node_id,
                            kind=HygieneKind.CLONE,
                            evidence=(
                                f"version {v.version_id} duplicates content "
                                f"first published by node {original_id}"
                            ),
                            action="revoked",
                        )
                    )
                    break  # one clone finding per node is enough

        flagged = {f.node_id for f in findings}

        # Fraud: verified failures, not one verified success.
        if self._stats is not None:
            for node_id, vs in versions.items():
                if node_id in flagged:
                    continue
                successes = failures = 0
                for v in vs:
                    s = self._stats.version_stats(v.version_id)
                    successes += s.successes
                    failures += s.failures
                if successes == 0 and failures >= self._fraud_min_failures:
                    findings.append(
                        HygieneFinding(
                            node_id=node_id,
                            kind=HygieneKind.FRAUD,
                            evidence=(
                                f"{failures} platform-verified failures and "
                                "not one verified success"
                            ),
                            action="restricted",
                        )
                    )
                    flagged.add(node_id)

        # Zombies: past the window, and no run was EVER bound to it.
        now = self._clock()
        for node_id, vs in versions.items():
            if node_id in flagged or not vs:
                continue
            if by_id[node_id].skill_id.startswith(_PLATFORM_NAMESPACE):
                continue  # the platform's own built-ins are exempt
            newest = max(v.published_at for v in vs)
            age_days = (now - newest).total_seconds() / 86_400.0
            if age_days <= self._zombie_after_days:
                continue
            bound = (
                self._attribution.bindings_for_versions(
                    [v.version_id for v in vs]
                )
                if self._attribution is not None
                else []
            )
            if bound:
                continue
            findings.append(
                HygieneFinding(
                    node_id=node_id,
                    kind=HygieneKind.ZOMBIE,
                    evidence=(
                        f"no run ever bound in {age_days:.0f} days since "
                        "its last version was published"
                    ),
                    action="restricted",
                )
            )
        return findings

    def sweep(self) -> list[HygieneFinding]:
        """Detect AND enforce — the policy everyone agreed to upfront.

        Clones are revoked (delisted) and their accounts restricted;
        fraud and zombie nodes are restricted. Idempotent: an already
        restricted/revoked node yields no new finding next time (revoked
        nodes leave inspection; restriction keeps the account as-is)."""
        findings = self.inspect()
        acted: list[HygieneFinding] = []
        for finding in findings:
            if finding.kind is HygieneKind.CLONE:
                node = self._registry.get_node(finding.node_id)
                if node is not None and node.revoked_at is None:
                    self._registry.update_node(
                        node.model_copy(update={"revoked_at": self._clock()})
                    )
            if self.is_restricted(finding.node_id):
                continue  # already under restriction: nothing new happened
            account = self._accounts.get(finding.node_id)
            if account is not None:
                self._accounts.upsert(
                    account.model_copy(
                        update={
                            "status": NodeStatus.RESTRICTED,
                            "updated_at": self._clock(),
                        }
                    )
                )
            acted.append(finding)
        return acted
