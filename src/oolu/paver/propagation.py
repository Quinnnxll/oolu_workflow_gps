"""Trigger propagation — one trigger, the whole web (W4).

When a user triggers a node (a webhook POST, a pulse schedule), that node
is the ANCHOR of a paved web: the Paver already surveyed which nodes feed
which, poured the connective code, and rehearsed the whole to full. W4
makes the fan-out happen — deterministically, boundedly, and only with
standing consent:

  * **Deferred, never in-request.** The anchor's own run completes and the
    POST returns; the cascade is ENQUEUED to the durable outbox and
    delivered by the lazy tick's drain. A trigger never rides one HTTP
    request down the whole web.
  * **Consent-gated, revocable.** Propagation stands per (tenant, web) —
    a user opts a web in, and revoking stops the NEXT propagation cold
    (re-checked at delivery, not just at enqueue).
  * **Bounded and deduped.** ``MAX_HOPS`` caps cross-web chaining depth,
    ``MAX_FIRES_PER_TRIGGER`` caps the fan-out, and a durable
    ``(trigger_stamp, target)`` claim fires each web/consumer ONCE per
    trigger — so a cycle A→B→A settles, across processes and restarts.
  * **Holds are respected, never routed around.** A web carrying a
    reserved action stops at the hold and surfaces ``awaiting_approval``;
    the approver's release resumes it. The Paver never bypasses a human.

The router is PURE orchestration over injected ports (``consent``,
``claim``, ``webs_at``, ``fire_web``, ``enqueue``, ``audit``), so it
unit-tests without a gateway; the gateway supplies the durable stores,
the outbox enqueue/drain, and the real market-free web firing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .contracts import RouteWeb

__all__ = [
    "MAX_FIRES_PER_TRIGGER",
    "MAX_HOPS",
    "PROPAGATION_TOPIC",
    "DeliveryRetry",
    "FireResult",
    "PropagationConsentStore",
    "TriggerClaimStore",
    "WebTriggerRouter",
]


class DeliveryRetry(RuntimeError):
    """A TRANSIENT delivery failure (W4.1): the web's fire errored in a way
    a later drain may succeed at. The router releases the fire-once claim
    and raises this so the outbox relay leaves the message PENDING
    (attempts incremented) — the at-least-once retry the outbox promises
    actually happens. A FINAL delivery (attempts exhausted) keeps the
    claim and settles the failure instead."""

# The outbox topic W4's enqueue stages under and the drain sink filters on
# — so a blind relay never sweeps the unrelated ``workflow.*`` checkpoint
# messages the durable service also stages.
PROPAGATION_TOPIC = "paver.propagation"

# Bounds: a cascade is deterministic AND finite. MAX_HOPS caps cross-web
# chaining depth (a fired web whose output triggers another web); a single
# fully-paved web fires in ONE contract run at hop 1, so most cascades are
# one hop. MAX_FIRES_PER_TRIGGER caps the total nodes a single root trigger
# may fan out to, the runaway backstop.
MAX_HOPS = 4
MAX_FIRES_PER_TRIGGER = 16


# --------------------------------------------------------------------------- #
# Consent — per (tenant, web), the same consent-first doctrine as the          #
# Paver's schedule, keyed per web and revocable.                               #
# --------------------------------------------------------------------------- #
_CONSENT_SCHEMA = """CREATE TABLE IF NOT EXISTS paver_propagation_consent (
    tenant TEXT NOT NULL,
    web_id TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY (tenant, web_id)
)"""


class PropagationConsentStore:
    """Standing, revocable consent to propagate a trigger through a paved
    web. ENABLING propagation for a web is the approved, audited act; every
    firing runs under that grant; revoking it stops the next propagation
    cold. Durable, so a fleet reads one answer."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_CONSENT_SCHEMA)

    def grant(self, tenant: str, web_id: str, *, granted_by: str, now: datetime) -> None:
        stamp = now.isoformat() if hasattr(now, "isoformat") else str(now)
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO paver_propagation_consent
                     (tenant, web_id, granted_by, granted_at, revoked_at)
                   VALUES (?, ?, ?, ?, NULL)
                   ON CONFLICT(tenant, web_id) DO UPDATE SET
                     granted_by = excluded.granted_by,
                     granted_at = excluded.granted_at,
                     revoked_at = NULL""",
                (tenant, web_id, granted_by, stamp),
            )

    def revoke(self, tenant: str, web_id: str, *, now: datetime) -> bool:
        stamp = now.isoformat() if hasattr(now, "isoformat") else str(now)
        with self._conn.transaction() as db:
            cursor = db.execute(
                """UPDATE paver_propagation_consent SET revoked_at = ?
                   WHERE tenant = ? AND web_id = ? AND revoked_at IS NULL""",
                (stamp, tenant, web_id),
            )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def stands(self, tenant: str, web_id: str) -> bool:
        """True iff propagation consent is granted AND not revoked — the
        gate re-checked at BOTH enqueue and delivery, so a revoke between
        the two stops the cascade."""
        with self._conn.lock:
            row = self._conn.db.execute(
                """SELECT revoked_at FROM paver_propagation_consent
                   WHERE tenant = ? AND web_id = ?""",
                (tenant, web_id),
            ).fetchone()
        return row is not None and row["revoked_at"] is None

    def list(self, tenant: str) -> list[dict]:
        with self._conn.lock:
            rows = self._conn.db.execute(
                """SELECT web_id, granted_by, granted_at, revoked_at
                   FROM paver_propagation_consent
                   WHERE tenant = ? ORDER BY web_id""",
                (tenant,),
            ).fetchall()
        return [
            {
                "web_id": row["web_id"],
                "granted_by": row["granted_by"],
                "granted_at": row["granted_at"],
                "revoked_at": row["revoked_at"],
                "stands": row["revoked_at"] is None,
            }
            for row in rows
        ]


# --------------------------------------------------------------------------- #
# Dedupe — a durable once-per-(trigger_stamp, target) claim, the pulse-        #
# occurrence election idiom: INSERT OR IGNORE, rowcount is the winner.         #
# --------------------------------------------------------------------------- #
_CLAIM_SCHEMA = """CREATE TABLE IF NOT EXISTS paver_trigger_claims (
    trigger_stamp TEXT NOT NULL,
    target TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    PRIMARY KEY (trigger_stamp, target)
)"""


class TriggerClaimStore:
    """The cross-process fire-once election. ``target`` is the unit that
    fires once per trigger — the ``web_id`` for a fast-path web run, the
    consumer ``node_id`` for a slow-path edge — so a cycle A→B→A fires each
    once and settles. Mirrors :class:`oolu.pulse.PulseStore.claim`."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_CLAIM_SCHEMA)

    def claim(self, trigger_stamp: str, target: str, *, now: datetime) -> bool:
        """True EXACTLY once per ``(trigger_stamp, target)`` across every
        process sharing the connection — the second caller sees the claim
        already taken (rowcount 0)."""
        stamp = now.isoformat() if hasattr(now, "isoformat") else str(now)
        with self._conn.transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO paver_trigger_claims
                     (trigger_stamp, target, fired_at) VALUES (?, ?, ?)""",
                (trigger_stamp, target, stamp),
            )
        return int(getattr(cursor, "rowcount", 0) or 0) > 0

    def release(self, trigger_stamp: str, target: str) -> None:
        """Give a claim back (W4.1) — called ONLY by the claim's own taker
        on a TRANSIENT fire failure, so the outbox's retry can re-take it
        on a later drain. A definitive outcome never releases: fire-once
        stands for everything that actually settled."""
        with self._conn.transaction() as db:
            db.execute(
                "DELETE FROM paver_trigger_claims"
                " WHERE trigger_stamp = ? AND target = ?",
                (trigger_stamp, target),
            )

    def count(self, trigger_stamp: str) -> int:
        """How many targets this trigger has fired so far — the durable
        per-trigger fan-out counter ``MAX_FIRES_PER_TRIGGER`` is enforced
        against (W4.1), shared across processes like the claims it counts."""
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT COUNT(*) AS n FROM paver_trigger_claims"
                " WHERE trigger_stamp = ?",
                (trigger_stamp,),
            ).fetchone()
        return int(row["n"] if row is not None else 0)


# --------------------------------------------------------------------------- #
# The router.                                                                  #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FireResult:
    """One web's delivery outcome. ``status``:
      * ``fired``       — the web ran (``fired`` = nodes, ``run_id`` set).
      * ``held``        — a reserved hop stopped it; ``awaiting_approval``.
      * ``no-consent``  — consent was revoked between enqueue and delivery.
      * ``skipped``     — already fired this trigger (dedupe), or nothing to do.
      * ``bounded``     — a hop/fire bound refused it.
      * ``failed``      — the web ran and failed."""

    web_id: str
    status: str
    fired: int = 0
    run_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class Enqueued:
    """One propagation message the anchor's completion staged."""

    tenant: str
    web_id: str
    anchor: str
    trigger_stamp: str
    hop: int = 1

    def payload(self) -> dict:
        return {
            "tenant": self.tenant,
            "web_id": self.web_id,
            "anchor": self.anchor,
            "trigger_stamp": self.trigger_stamp,
            "hop": self.hop,
        }


class WebTriggerRouter:
    """Fan one trigger out across a paved web — deferred, bounded, deduped,
    consent-gated. Pure orchestration over injected ports:

      * ``consent(tenant, web_id) -> bool`` — does propagation stand.
      * ``claim(trigger_stamp, target) -> bool`` — durable fire-once.
      * ``webs_at(tenant, node_id) -> list[RouteWeb]`` — webs anchored here.
      * ``fire_web(tenant, web_id, trigger_stamp, hop) -> FireResult`` —
        run the web (fast/slow path, reserved-hold aware); the gateway's.
      * ``enqueue(payload) -> None`` — stage durable propagation work.
      * ``audit(event, payload) -> None`` — the hash-chained record.
    """

    def __init__(
        self,
        *,
        consent: Callable[[str, str], bool],
        claim: Callable[[str, str], bool],
        webs_at: Callable[[str, str], list[RouteWeb]],
        fire_web: Callable[[str, str, str, int], FireResult],
        enqueue: Callable[[dict], None] | None = None,
        audit: Callable[[str, dict], None] | None = None,
        release: Callable[[str, str], None] | None = None,
        fires: Callable[[str], int] | None = None,
        max_hops: int = MAX_HOPS,
        max_fires: int = MAX_FIRES_PER_TRIGGER,
    ) -> None:
        self._consent = consent
        self._claim = claim
        self._webs_at = webs_at
        self._fire_web = fire_web
        self._enqueue = enqueue
        self._audit = audit
        # W4.1 ports: ``release(stamp, target)`` gives a claim back on a
        # TRANSIENT failure (so the outbox retry can re-take it);
        # ``fires(stamp) -> int`` is the durable per-trigger fan-out
        # counter MAX_FIRES_PER_TRIGGER is enforced against. Without a
        # release port a transient failure settles as final (no retry —
        # honest, never lost silently); without a fires port only the
        # enqueue-side cap holds.
        self._release = release
        self._fires = fires
        self._max_hops = max(1, max_hops)
        self._max_fires = max(1, max_fires)

    # ------------------------------------------------------------------ #
    def on_trigger(
        self, tenant: str, node_id: str, run_id: str, *, hop: int = 1
    ) -> list[Enqueued]:
        """The post-terminal hook — ENQUEUE-ONLY, never fires in-request.
        For each web anchored at ``node_id`` whose propagation consent
        stands, mint a trigger stamp from the anchor run and stage one
        durable propagation message. Returns what was enqueued (also
        staged via the ``enqueue`` port when given). A trigger that anchors
        no consented web enqueues nothing — the POST just returns."""
        if hop > self._max_hops:
            return []
        stamp = f"{node_id}:{run_id}"
        enqueued: list[Enqueued] = []
        for web in self._webs_at(tenant, node_id):
            # The fan-out cap, enforced at the enqueue side too (W4.1): one
            # trigger stages at most max_fires webs — the runaway backstop
            # is real, not declared.
            if len(enqueued) >= self._max_fires:
                if self._audit is not None:
                    self._audit(
                        "paver.propagation_bounded",
                        {
                            "run_id": "paver:propagation",
                            "tenant": tenant,
                            "web_id": web.web_id,
                            "trigger_stamp": stamp,
                            "reason": f"max fires per trigger ({self._max_fires})",
                        },
                    )
                break
            if web.web_id in {e.web_id for e in enqueued}:
                continue
            if not self._consent(tenant, web.web_id):
                continue
            message = Enqueued(
                tenant=tenant, web_id=web.web_id, anchor=node_id,
                trigger_stamp=stamp, hop=hop,
            )
            if self._enqueue is not None:
                self._enqueue(message.payload())
            enqueued.append(message)
            if self._audit is not None:
                self._audit(
                    "paver.propagation_enqueued",
                    {
                        "run_id": "paver:propagation",
                        "tenant": tenant,
                        "web_id": web.web_id,
                        "anchor": node_id,
                        "trigger_stamp": stamp,
                        "hop": hop,
                    },
                )
        return enqueued

    # ------------------------------------------------------------------ #
    def deliver(self, payload: dict, *, final: bool = False) -> FireResult:
        """The drain sink — deliver ONE staged propagation message. Bounds
        first, then consent RE-CHECKED (a revoke since enqueue stops it
        cold), then a durable fire-once claim, then the web fires. Idempotent:
        a re-delivered message whose claim was already taken is a no-op, so
        the at-least-once outbox never fires a web twice.

        A ``failed`` fire is TRANSIENT unless ``final`` (W4.1): the claim
        is RELEASED and :class:`DeliveryRetry` raised, so the outbox relay
        leaves the message pending and a later drain retries — a flaky run
        never silently loses the cascade. The caller passes ``final=True``
        when the message's retry budget is spent; the failure then settles
        (claim kept, audited) instead of retrying forever."""
        tenant = str(payload.get("tenant") or "")
        web_id = str(payload.get("web_id") or "")
        stamp = str(payload.get("trigger_stamp") or "")
        hop = int(payload.get("hop") or 1)
        if not tenant or not web_id or not stamp:
            return FireResult(web_id, "skipped", reason="malformed message")
        if hop > self._max_hops:
            return FireResult(web_id, "bounded", reason=f"max hops ({self._max_hops})")
        # The fan-out cap (W4.1): a trigger that already fired max_fires
        # targets fires no more — the durable claims table is the counter,
        # so the bound holds across processes.
        if self._fires is not None and self._fires(stamp) >= self._max_fires:
            self._audit_fire("paver.propagation_bounded", tenant, web_id, stamp)
            return FireResult(
                web_id, "bounded",
                reason=f"max fires per trigger ({self._max_fires})",
            )
        # Consent re-checked at delivery — the revoke-stops-the-next gate.
        if not self._consent(tenant, web_id):
            self._audit_fire("paver.propagation_no_consent", tenant, web_id, stamp)
            return FireResult(web_id, "no-consent", reason="consent revoked")
        # Fire-once: the web fires at most once per trigger, across processes.
        if not self._claim(stamp, web_id):
            return FireResult(web_id, "skipped", reason="already fired this trigger")
        result = self._fire_web(tenant, web_id, stamp, hop)
        if result.status == "failed" and not final and self._release is not None:
            # Transient: give the claim back and make the relay RETRY —
            # a returned failure would be marked sent and silently lost
            # (the exact at-least-once promise the outbox exists for).
            self._release(stamp, web_id)
            self._audit_fire("paver.propagation_retry", tenant, web_id, stamp)
            raise DeliveryRetry(
                f"web {web_id} fire failed transiently: {result.reason[:200]}"
            )
        event = {
            "fired": "paver.propagation_fired",
            "held": "paver.propagation_held",
        }.get(result.status, "paver.propagation_settled")
        if self._audit is not None:
            self._audit(
                event,
                {
                    "run_id": "paver:propagation",
                    "tenant": tenant,
                    "web_id": web_id,
                    "trigger_stamp": stamp,
                    "hop": hop,
                    "status": result.status,
                    "fired": result.fired,
                    "node_run": result.run_id,
                    "reason": result.reason[:200],
                },
            )
        return result

    # ------------------------------------------------------------------ #
    def _audit_fire(self, event: str, tenant: str, web_id: str, stamp: str) -> None:
        if self._audit is not None:
            self._audit(
                event,
                {
                    "run_id": "paver:propagation",
                    "tenant": tenant,
                    "web_id": web_id,
                    "trigger_stamp": stamp,
                },
            )
