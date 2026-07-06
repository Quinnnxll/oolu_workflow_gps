"""RemoteKnowledgeClient — the central-server drop-in behind the KnowledgeClient seam.

Same Protocol as Noop/Local, so it swaps in by config. It composes a
``LocalKnowledgeClient`` (the read cache + local learned store) with best-effort
background sync to a central server. The design encodes the crowd-trust boundary at
every step:

READS come from the local SQLite cache — never a live network call — so the resolver
is never blocked on the server. A background thread pulls crowd hints into a quarantine
table; locally-learned hints live in the composed local store.

WRITES go to the local store immediately (so learning persists offline) and are queued
for best-effort async upload after each successful run. Every uploaded lesson is
re-scrubbed client-side; the server is never trusted to scrub.

INGEST is doubly gated. (1) Server aggregation as a threshold filter: only crowd hints
whose server-reported aggregate clears ``min_server_reports`` / ``min_server_success_rate``
are taken at all. (2) Local quarantine with progressive promotion: accepted hints are
stored as ``CROWD`` (re-stamped on arrival, regardless of what the server claims) and are
NOT surfaced to the resolver until either local corroboration promotes them, or the
operator opts into ``allow_unverified_crowd_install`` (accepting the supply-chain
trade-off of installing crowd-suggested package names, bounded by the server threshold).

EVERYTHING NETWORK FAILS OPEN. Any transport, auth, or parse error is swallowed; the
engine continues on cached/local knowledge. The remote layer is an accelerator, never a
dependency.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..models import (
    DependencyHint,
    ErrorClass,
    ErrorPattern,
    KnowledgeSource,
    RecalcStrategy,
)
from ..persistence import Migration, migrate
from .auth import TokenProvider
from .client import LocalKnowledgeClient
from .scrubbing import is_safe_identifier, is_safe_to_store, scrub

_log = logging.getLogger("oolu.knowledge.remote")


def _create_quarantine_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crowd_quarantine (
            import_name          TEXT NOT NULL,
            package_name         TEXT NOT NULL,
            server_success       INTEGER NOT NULL DEFAULT 0,
            server_total         INTEGER NOT NULL DEFAULT 0,
            local_corroborations INTEGER NOT NULL DEFAULT 0,
            promoted             INTEGER NOT NULL DEFAULT 0,
            first_seen           TEXT NOT NULL,
            last_seen            TEXT NOT NULL,
            PRIMARY KEY (import_name, package_name)
        )
        """
    )


# Ordered schema history for the crowd-quarantine ledger. Append-only.
QUARANTINE_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        up=_create_quarantine_table,
        down=lambda conn: conn.execute("DROP TABLE IF EXISTS crowd_quarantine"),
    ),
)


# --------------------------------------------------------------------------- #
# Transport seam.                                                             #
# --------------------------------------------------------------------------- #
class TransportError(Exception):
    """Any network/HTTP/parse failure. Always caught and treated as fail-open."""


@runtime_checkable
class Transport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json_body: dict | None = None,
        timeout: float = 10.0,
    ) -> dict: ...


class UrllibTransport:
    """Real HTTP transport on the standard library (no third-party deps)."""

    def request_json(
        self, method, url, *, headers=None, json_body=None, timeout=10.0
    ) -> dict:
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            raise TransportError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Config.                                                                     #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RemoteConfig:
    base_url: str
    sync_interval_s: float = 300.0
    request_timeout_s: float = 10.0
    # Gate 1 — server aggregation as a threshold filter.
    min_server_reports: int = 5
    min_server_success_rate: float = 0.8
    # Gate 2 — local quarantine + progressive promotion.
    promotion_corroborations: int = 1
    allow_unverified_crowd_install: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Client.                                                                      #
# --------------------------------------------------------------------------- #
class RemoteKnowledgeClient:
    """Crowd-aware knowledge client. Satisfies the KnowledgeClient Protocol."""

    def __init__(
        self,
        config: RemoteConfig,
        token_provider: TokenProvider,
        *,
        local: LocalKnowledgeClient | None = None,
        transport: Transport | None = None,
        local_db_path: str = ":memory:",
        quarantine_db_path: str = ":memory:",
        start_background: bool = True,
    ):
        self._cfg = config
        self._token = token_provider
        self._transport = transport or UrllibTransport()
        self._local = local or LocalKnowledgeClient(local_db_path)

        self._qlock = threading.Lock()
        self._qconn = sqlite3.connect(quarantine_db_path, check_same_thread=False)
        self._qconn.row_factory = sqlite3.Row
        with self._qlock:
            migrate(self._qconn, QUARANTINE_MIGRATIONS, label="crowd_quarantine")

        self._upload_q: deque[dict] = deque()
        self._uplock = threading.Lock()

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if start_background:
            self.start()

    # --- background lifecycle ---------------------------------------- #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="oolu-knowledge-sync", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._cfg.sync_interval_s):
            self.sync_now()  # fully guarded; never raises

    # --- KnowledgeClient: reads (cache only, never network) ----------- #
    def all_dependency_hints(self) -> list[DependencyHint]:
        return self._local.all_dependency_hints() + self._visible_crowd_hints()

    def get_dependency_hints(self, import_name: str) -> list[DependencyHint]:
        crowd = [h for h in self._visible_crowd_hints() if h.import_name == import_name]
        return self._local.get_dependency_hints(import_name) + crowd

    def get_error_patterns(self, error_class: ErrorClass) -> list[ErrorPattern]:
        return self._local.get_error_patterns(error_class)

    # --- KnowledgeClient: writes (local now, upload best-effort) ------- #
    def record_dependency_success(
        self, import_name: str, package_name: str, *, version: str | None = None
    ) -> None:
        self._local.record_dependency_success(
            import_name, package_name, version=version
        )
        self._corroborate(import_name, package_name)
        self._enqueue(
            {
                "type": "dependency",
                "import_name": import_name,
                "package_name": package_name,
                "outcome": "success",
                "version": version,
            }
        )

    def record_dependency_failure(self, import_name: str, package_name: str) -> None:
        self._local.record_dependency_failure(import_name, package_name)
        self._enqueue(
            {
                "type": "dependency",
                "import_name": import_name,
                "package_name": package_name,
                "outcome": "failure",
                "version": None,
            }
        )

    def record_error_pattern(
        self,
        error_class: ErrorClass,
        error_signature: str,
        strategy: RecalcStrategy,
        *,
        success: bool = True,
    ) -> None:
        self._local.record_error_pattern(
            error_class, error_signature, strategy, success=success
        )
        self._enqueue(
            {
                "type": "error_pattern",
                "error_class": error_class.value,
                "error_signature": scrub(error_signature),
                "strategy": strategy.value,
                "outcome": "success" if success else "failure",
            }
        )

    # --- promotion: local corroboration of a quarantined crowd hint --- #
    def _corroborate(self, import_name: str, package_name: str) -> None:
        with self._qlock:
            row = self._qconn.execute(
                "SELECT local_corroborations FROM crowd_quarantine WHERE import_name=? AND package_name=?",
                (import_name, package_name),
            ).fetchone()
            if row is None:
                return
            corro = row["local_corroborations"] + 1
            promoted = 1 if corro >= self._cfg.promotion_corroborations else 0
            self._qconn.execute(
                "UPDATE crowd_quarantine SET local_corroborations=?, promoted=?, last_seen=? "
                "WHERE import_name=? AND package_name=?",
                (corro, promoted, _now(), import_name, package_name),
            )
            self._qconn.commit()

    def _visible_crowd_hints(self) -> list[DependencyHint]:
        """Crowd hints the resolver is allowed to see: promoted ones (counts derived
        from local corroboration so they clear the trust floor), plus — only if the
        operator opted in — high-aggregate quarantined ones (counts from the server)."""
        with self._qlock:
            rows = self._qconn.execute("SELECT * FROM crowd_quarantine").fetchall()
        hints: list[DependencyHint] = []
        for r in rows:
            if r["promoted"]:
                success = max(r["local_corroborations"], 1)
                failure = 0
            elif self._cfg.allow_unverified_crowd_install:
                success = r["server_success"]
                failure = max(r["server_total"] - r["server_success"], 0)
            else:
                continue  # quarantined and not opted-in => invisible to the resolver
            hints.append(
                DependencyHint(
                    import_name=r["import_name"],
                    package_name=r["package_name"],
                    source=KnowledgeSource.CROWD,
                    success_count=success,
                    failure_count=failure,
                )
            )
        return hints

    # --- upload queue ------------------------------------------------- #
    def _enqueue(self, lesson: dict) -> None:
        with self._uplock:
            self._upload_q.append(lesson)

    # --- sync (fail-open everywhere) ---------------------------------- #
    def sync_now(self) -> None:
        """Flush queued uploads and pull crowd hints. Never raises."""
        try:
            self._flush_uploads()
        except Exception as exc:  # noqa: BLE001
            _log.debug("upload flush failed (fail-open): %s", exc)
        try:
            self._pull_crowd()
        except Exception as exc:  # noqa: BLE001
            _log.debug("crowd pull failed (fail-open): %s", exc)

    def _auth_headers(self) -> dict | None:
        try:
            return {"Authorization": f"Bearer {self._token.get_token()}"}
        except Exception as exc:  # noqa: BLE001 - missing/expired token => skip network
            _log.debug("token unavailable (fail-open): %s", exc)
            return None

    def _flush_uploads(self) -> None:
        with self._uplock:
            if not self._upload_q:
                return
            pending = list(self._upload_q)
        headers = self._auth_headers()
        if headers is None:
            return  # keep queue for a later cycle
        clean = [lesson for lesson in pending if self._lesson_is_safe(lesson)]
        if clean:
            self._transport.request_json(
                "POST",
                f"{self._cfg.base_url}/v1/lessons",
                headers=headers,
                json_body={"lessons": clean},
                timeout=self._cfg.request_timeout_s,
            )
        # Success (or nothing safe to send): drop exactly what we took.
        with self._uplock:
            for _ in range(len(pending)):
                if self._upload_q:
                    self._upload_q.popleft()

    @staticmethod
    def _lesson_is_safe(lesson: dict) -> bool:
        if lesson.get("type") == "dependency":
            return is_safe_identifier(
                lesson.get("import_name", "")
            ) and is_safe_identifier(lesson.get("package_name", ""))
        if lesson.get("type") == "error_pattern":
            return is_safe_to_store(lesson.get("error_signature", ""))
        return False

    def _pull_crowd(self) -> None:
        headers = self._auth_headers()
        if headers is None:
            return
        data = self._transport.request_json(
            "GET",
            f"{self._cfg.base_url}/v1/dependency-hints",
            headers=headers,
            timeout=self._cfg.request_timeout_s,
        )
        for raw in data.get("hints", []):
            self._ingest_crowd_hint(raw)

    def _ingest_crowd_hint(self, raw: dict) -> None:
        import_name = str(raw.get("import_name", ""))
        package_name = str(raw.get("package_name", ""))
        # Hard identifier gate first — never store a path/secret as a "package".
        if not (is_safe_identifier(import_name) and is_safe_identifier(package_name)):
            return
        total = int(raw.get("server_total", raw.get("total", 0)) or 0)
        success = int(raw.get("server_success", raw.get("success", 0)) or 0)
        # Gate 1: server aggregation as a threshold filter.
        if total < self._cfg.min_server_reports:
            return
        if total <= 0 or (success / total) < self._cfg.min_server_success_rate:
            return
        # Gate 2: store quarantined as CROWD (source re-stamped, server never trusted to
        # mint LOCAL trust). Existing local corroboration/promotion is preserved.
        now = _now()
        with self._qlock:
            self._qconn.execute(
                """
                INSERT INTO crowd_quarantine
                    (import_name, package_name, server_success, server_total, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(import_name, package_name) DO UPDATE SET
                    server_success = excluded.server_success,
                    server_total   = excluded.server_total,
                    last_seen      = excluded.last_seen
                """,
                (import_name, package_name, success, total, now, now),
            )
            self._qconn.commit()

    # --- adapters / lifecycle ---------------------------------------- #
    def as_hint_provider(self):
        return lambda _intent: self.all_dependency_hints()

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        try:
            self.sync_now()  # last best-effort flush
        except Exception:  # noqa: BLE001
            pass
        with self._qlock:
            self._qconn.close()
        self._local.close()

    def __enter__(self) -> "RemoteKnowledgeClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
