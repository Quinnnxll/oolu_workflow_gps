"""The sweep — reclaiming a shared content-addressed store, safely.

The bundle tiers made boot fast; nothing yet made idle *lean*. The CAS
only ever grew: every edited node re-freezes to a new bundle and leaves
the old manifest behind, and blobs for files no node references anymore
sit forever. At the scale the bundle work targets — global nodes cloning
professional libraries — that unbounded growth is the idle cost the whole
architecture exists to avoid.

The reason a sweep is delicate here, and the reason this module exists at
all, is that the CAS is **shared**. The same object store holds bundle
blobs, the file drawer's blobs, and the CAD hand's exports — and because
it is content-addressed, identical bytes are ONE object. A node whose
``src/main.py`` happens to equal a file in someone's drawer shares that
blob; deleting it because the bundle died would corrupt the drawer. So
the one rule this sweep never breaks:

    a blob is deleted ONLY if it is referenced by NO source, and a blob
    this sweep cannot attribute at all is kept.

Reachability is therefore a UNION of every subsystem that puts bytes in
the store. Each declares its live refs through :class:`ReferenceSource`;
the sweep marks their union and deletes only what falls outside it —
never guessing, never deleting to reclaim space it is unsure about. A
generous age grace spares just-written blobs (a freeze in flight, a run
starting), and the sweep is dry-run-first: :meth:`CasSweep.inspect`
reports exactly what :meth:`CasSweep.collect` would remove, so an
operator sees the plan before anything is deleted. The durable truth a
deletion might cost is only a re-freeze; a mistaken deletion of a
referenced blob would be data loss, which is why the rule is conservative
in exactly that direction.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class ReferenceSource(Protocol):
    """One subsystem's promise of what it still needs in the CAS.

    ``live_blob_refs`` returns every ``sha256:...`` ref the source would be
    corrupted to lose. The sweep unions these; a ref in ANY source is
    reachable. A source that cannot cheaply enumerate its refs must say so
    by not being a source — the sweep then has no authority to delete and
    reports the store as unsweepable rather than risking that source's
    data."""

    name: str

    def live_blob_refs(self) -> set[str]: ...


@dataclass(frozen=True)
class CallableSource:
    """A reference source from a plain callable — the common adapter."""

    name: str
    _refs: Callable[[], set[str]]

    def live_blob_refs(self) -> set[str]:
        return set(self._refs())


@dataclass(frozen=True)
class SweepPlan:
    """What a sweep would (or did) reclaim."""

    dead_manifests: tuple[str, ...] = ()  # bundle_ids whose trees are gone
    orphan_blobs: tuple[str, ...] = ()  # CAS refs referenced by no source
    reclaimed_bytes: int = 0
    kept_blobs: int = 0  # blobs spared (referenced, or within the grace)
    tier_discards: int = 0  # warm tars / materialized trees purged with the dead
    sources: tuple[str, ...] = ()  # the reference sources consulted
    applied: bool = False  # False for inspect (dry run), True for collect

    def as_dict(self) -> dict:
        return {
            "dead_manifests": list(self.dead_manifests),
            "orphan_blobs": list(self.orphan_blobs),
            "reclaimed_bytes": self.reclaimed_bytes,
            "kept_blobs": self.kept_blobs,
            "tier_discards": self.tier_discards,
            "sources": list(self.sources),
            "applied": self.applied,
        }


class CasSweep:
    """Mark-and-sweep over the shared object store, union-of-sources safe.

    Give it the bundle store, the CAS, and every reference source that puts
    bytes in that CAS. ``inspect`` computes the plan without touching a
    thing; ``collect`` computes the same plan and applies it. Blobs younger
    than ``grace_seconds`` are always kept — a freeze or a run in flight
    has written bytes it has not yet referenced.
    """

    def __init__(
        self,
        bundle_store,  # runtime.bundle.BundleStore
        artifacts,  # durable.FilesystemArtifactStore (must offer refs())
        *,
        sources: list[ReferenceSource] | None = None,
        live_bundle_ids: Callable[[], set[str]] | None = None,
        # Accelerator tiers (WarmBundleTier, MaterializedBundleDir, or
        # anything with discard(bundle_id)) to purge alongside a dead
        # manifest — on a FLEET's shared materialized root this is the only
        # remover there is, since per-host eviction is off in shared mode.
        tiers: list | None = None,
        grace_seconds: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._bundles = bundle_store
        self._artifacts = artifacts
        self._sources = list(sources or [])
        # Which bundles are still referenced by a live node function. A
        # bundle absent from this set (and past the grace) is dead: its
        # manifest is dropped and its bytes become sweep candidates.
        self._live_bundle_ids = live_bundle_ids or (lambda: set())
        self._tiers = list(tiers or [])
        self._grace = grace_seconds
        self._clock = clock

    # ------------------------------------------------------------------ #
    def inspect(self) -> SweepPlan:
        return self._run(apply=False)

    def collect(self) -> SweepPlan:
        return self._run(apply=True)

    # ------------------------------------------------------------------ #
    def _run(self, *, apply: bool) -> SweepPlan:
        if not hasattr(self._artifacts, "refs"):
            # An object store without cheap enumeration: no authority to
            # sweep it. Report nothing rather than guess.
            return SweepPlan(sources=tuple(s.name for s in self._sources))

        now = self._clock()
        live_bundle_ids = set(self._live_bundle_ids())

        # Dead manifests: a stored bundle no live node references.
        manifests = self._bundles.manifests()
        dead_ids = [
            manifest.bundle_id
            for manifest, _created in manifests
            if manifest.bundle_id not in live_bundle_ids
        ]

        # The sweep's AUTHORITY is limited to bytes the bundle layer itself
        # introduced for now-dead trees: candidates are exactly the blobs a
        # dead bundle referenced. A blob no dead bundle referenced — a CAD
        # export, a durable artifact, a drawer-only upload — is never a
        # candidate, so this sweep cannot touch what it did not create.
        candidates = self._bundles.blob_refs_of(dead_ids)
        if not candidates:
            discards = self._purge_tiers(dead_ids) if apply else 0
            if apply and dead_ids:
                self._bundles.forget(dead_ids)
            return SweepPlan(
                dead_manifests=tuple(sorted(dead_ids)),
                tier_discards=discards,
                sources=tuple(s.name for s in self._sources),
                applied=apply,
            )

        # The reachable union that SPARES a candidate: every source's live
        # refs, plus every live bundle's blobs. A candidate shared with a
        # live bundle or any source is kept — content addressing means one
        # object, and losing it would corrupt that other holder.
        reachable: set[str] = set(self._bundles.blob_refs_of(live_bundle_ids))
        for source in self._sources:
            reachable |= source.live_blob_refs()

        # Ages of the candidate blobs, from the store's own walk. A blob we
        # cannot see the age of is treated as recent (kept) — never deleted
        # on a guess.
        ages = {
            ref: (size, mtime)
            for ref, size, mtime in self._artifacts.refs()
            if ref in candidates
        }

        orphans: list[str] = []
        reclaimed = 0
        kept = 0
        for ref in candidates:
            if ref in reachable:
                kept += 1
                continue
            meta = ages.get(ref)
            if meta is None or (now - meta[1]) < self._grace:
                kept += 1  # unseen age or within the grace window
                continue
            orphans.append(ref)
            reclaimed += meta[0]

        discards = 0
        if apply:
            if dead_ids:
                self._bundles.forget(dead_ids)
            for ref in orphans:
                self._artifacts.delete(ref)
            discards = self._purge_tiers(dead_ids)

        return SweepPlan(
            dead_manifests=tuple(sorted(dead_ids)),
            orphan_blobs=tuple(sorted(orphans)),
            reclaimed_bytes=reclaimed,
            kept_blobs=kept,
            tier_discards=discards,
            sources=tuple(s.name for s in self._sources),
            applied=apply,
        )

    def _purge_tiers(self, dead_ids) -> int:
        """Drop the accelerator copies of dead bundles — warm tars and
        materialized trees. Each tier's own discard() applies its own
        safety (the materialized dir refuses within its grace window), so
        the sweep asks, never forces."""
        discards = 0
        for bundle_id in dead_ids:
            for tier in self._tiers:
                try:
                    if tier.discard(bundle_id):
                        discards += 1
                except OSError:  # a busy NFS dir waits for the next pass
                    continue
        return discards


# --------------------------------------------------------------------------- #
# The Routine: a standing, consented, fleet-safe schedule for the sweep.       #
# --------------------------------------------------------------------------- #
MIN_SWEEP_INTERVAL_HOURS = 1.0
DEFAULT_SWEEP_INTERVAL_HOURS = 24.0

_SCHEDULE_SCHEMA = """CREATE TABLE IF NOT EXISTS sweep_schedule (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    interval_hours REAL NOT NULL,
    granted_by TEXT NOT NULL,
    tenant TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    last_started_at TEXT,
    last_finished_at TEXT,
    last_summary_json TEXT,
    last_error TEXT
)"""


class SweepScheduleStore:
    """The sweep's recurring Routine — consent that stands until revoked.

    The manual sweep is approve-gated because it deletes; a SCHEDULE means
    unattended firings, so the consent moves up a level: ENABLING the
    schedule is the approved, audited act (who, when, how often), each
    firing runs under that standing grant, and revoking it stops the next
    firing cold. The row is durable and single (id=1): on a fleet sharing
    one database, every host reads the same Routine.

    Fleet-safe by construction: ``claim_due`` advances ``last_started_at``
    in ONE conditional UPDATE, so when many hosts tick at once exactly one
    wins the firing and the rest see the claim already taken — no locks,
    no coordinator, the database's own atomicity.
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEDULE_SCHEMA)

    def enable(
        self,
        *,
        interval_hours: float,
        granted_by: str,
        tenant: str,
        now: datetime,
    ) -> dict:
        """Set (or retune) the standing schedule. The caller has already
        passed the approve gate — this records whose consent stands."""
        interval = max(MIN_SWEEP_INTERVAL_HOURS, float(interval_hours))
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO sweep_schedule
                     (id, enabled, interval_hours, granted_by, tenant,
                      granted_at, last_started_at)
                   VALUES (1, 1, ?, ?, ?, ?, NULL)
                   ON CONFLICT(id) DO UPDATE SET
                     enabled = 1,
                     interval_hours = excluded.interval_hours,
                     granted_by = excluded.granted_by,
                     tenant = excluded.tenant,
                     granted_at = excluded.granted_at""",
                (interval, granted_by, tenant, now.isoformat()),
            )
        return self.view()

    def disable(self) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "UPDATE sweep_schedule SET enabled = 0 WHERE id = 1 AND enabled = 1"
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def view(self) -> dict | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT * FROM sweep_schedule WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        summary = row["last_summary_json"]
        return {
            "enabled": bool(row["enabled"]),
            "interval_hours": row["interval_hours"],
            "granted_by": row["granted_by"],
            "tenant": row["tenant"],
            "granted_at": row["granted_at"],
            "last_started_at": row["last_started_at"],
            "last_finished_at": row["last_finished_at"],
            "last_summary": json.loads(summary) if summary else None,
            "last_error": row["last_error"],
        }

    def claim_due(self, now: datetime) -> bool:
        """True iff the schedule is enabled, the interval has elapsed, and
        THIS caller won the firing — one conditional UPDATE, so a fleet of
        ticking hosts fires exactly once per due interval."""
        view = self.view()
        if view is None or not view["enabled"]:
            return False
        cutoff = (
            now - timedelta(hours=float(view["interval_hours"]))
        ).isoformat()
        with self._conn.transaction() as db:
            cursor = db.execute(
                """UPDATE sweep_schedule
                   SET last_started_at = ?
                   WHERE id = 1 AND enabled = 1
                     AND (last_started_at IS NULL OR last_started_at <= ?)""",
                (now.isoformat(), cutoff),
            )
            return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def record_result(
        self,
        now: datetime,
        *,
        summary: dict | None = None,
        error: str | None = None,
    ) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """UPDATE sweep_schedule
                   SET last_finished_at = ?, last_summary_json = ?, last_error = ?
                   WHERE id = 1""",
                (
                    now.isoformat(),
                    json.dumps(summary) if summary is not None else None,
                    error,
                ),
            )
