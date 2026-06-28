"""In-memory, local SQLite, and serialization-boundary skill stores."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .models import ExecutionOutcome, SKILL_SCHEMA_VERSION, ReusableSkill


class InMemorySkillStore:
    def __init__(self):
        self._skills: dict[str, ReusableSkill] = {}

    def save(self, skill: ReusableSkill) -> None:
        self._skills[skill.id] = skill

    def get(self, skill_id: str) -> ReusableSkill | None:
        return self._skills.get(skill_id)

    def list(self, *, limit: int = 100) -> list[ReusableSkill]:
        return sorted(
            self._skills.values(), key=lambda item: item.updated_at, reverse=True
        )[:limit]

    def delete(self, skill_id: str) -> bool:
        return self._skills.pop(skill_id, None) is not None

    def close(self) -> None:
        return None


class RemoteMockSkillStore:
    """Models a network boundary by storing only serialized JSON payloads."""

    def __init__(self):
        self._payloads: dict[str, str] = {}

    def save(self, skill: ReusableSkill) -> None:
        self._payloads[skill.id] = skill.model_dump_json()

    def get(self, skill_id: str) -> ReusableSkill | None:
        payload = self._payloads.get(skill_id)
        return (
            ReusableSkill.model_validate_json(payload) if payload is not None else None
        )

    def list(self, *, limit: int = 100) -> list[ReusableSkill]:
        skills = [
            ReusableSkill.model_validate_json(payload)
            for payload in self._payloads.values()
        ]
        return sorted(skills, key=lambda item: item.updated_at, reverse=True)[:limit]

    def delete(self, skill_id: str) -> bool:
        return self._payloads.pop(skill_id, None) is not None

    def close(self) -> None:
        return None


class InMemoryExecutionStore:
    def __init__(self):
        self._outcomes: dict[tuple[str, str], ExecutionOutcome] = {}

    def save(self, outcome: ExecutionOutcome) -> None:
        self._outcomes.setdefault((outcome.skill_id, outcome.idempotency_key), outcome)

    def get(self, skill_id: str, idempotency_key: str) -> ExecutionOutcome | None:
        return self._outcomes.get((skill_id, idempotency_key))

    def close(self) -> None:
        return None


class RemoteMockExecutionStore:
    def __init__(self):
        self._payloads: dict[tuple[str, str], str] = {}

    def save(self, outcome: ExecutionOutcome) -> None:
        self._payloads.setdefault(
            (outcome.skill_id, outcome.idempotency_key), outcome.model_dump_json()
        )

    def get(self, skill_id: str, idempotency_key: str) -> ExecutionOutcome | None:
        payload = self._payloads.get((skill_id, idempotency_key))
        return ExecutionOutcome.model_validate_json(payload) if payload else None

    def close(self) -> None:
        return None


class LocalSkillStore:
    """Versioned local persistence with migrations isolated behind the SkillStore port."""

    def __init__(self, path: str | Path):
        db_path = str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        if db_path != ":memory:":
            Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS skill_store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = self._db.execute(
                "SELECT value FROM skill_store_meta WHERE key = 'schema_version'"
            ).fetchone()
            version = int(row["value"]) if row else 0
            if version > SKILL_SCHEMA_VERSION:
                raise RuntimeError(
                    f"skill database schema {version} is newer than supported {SKILL_SCHEMA_VERSION}"
                )
            if version < 1:
                self._db.execute(
                    """CREATE TABLE skills (
                           skill_id TEXT PRIMARY KEY,
                           schema_version INTEGER NOT NULL,
                           name TEXT NOT NULL,
                           application TEXT NOT NULL,
                           adapter TEXT NOT NULL,
                           payload_json TEXT NOT NULL,
                           updated_at TEXT NOT NULL
                       )"""
                )
                self._db.execute(
                    """INSERT INTO skill_store_meta (key, value) VALUES ('schema_version', '1')
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value"""
                )

    def save(self, skill: ReusableSkill) -> None:
        if skill.schema_version != SKILL_SCHEMA_VERSION:
            raise ValueError(
                f"cannot store skill schema {skill.schema_version}; expected {SKILL_SCHEMA_VERSION}"
            )
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO skills (
                       skill_id, schema_version, name, application, adapter,
                       payload_json, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(skill_id) DO UPDATE SET
                       schema_version = excluded.schema_version,
                       name = excluded.name,
                       application = excluded.application,
                       adapter = excluded.adapter,
                       payload_json = excluded.payload_json,
                       updated_at = excluded.updated_at""",
                (
                    skill.id,
                    skill.schema_version,
                    skill.name,
                    skill.signature.application,
                    skill.signature.adapter,
                    skill.model_dump_json(),
                    skill.updated_at.isoformat(),
                ),
            )

    def get(self, skill_id: str) -> ReusableSkill | None:
        with self._lock:
            row = self._db.execute(
                "SELECT payload_json FROM skills WHERE skill_id = ?", (skill_id,)
            ).fetchone()
        return ReusableSkill.model_validate_json(row["payload_json"]) if row else None

    def list(self, *, limit: int = 100) -> list[ReusableSkill]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload_json FROM skills ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [ReusableSkill.model_validate_json(row["payload_json"]) for row in rows]

    def delete(self, skill_id: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM skills WHERE skill_id = ?", (skill_id,)
            )
        return cursor.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._db.close()


class LocalExecutionStore:
    """Durable idempotency ledger; safe to place beside the skill tables."""

    def __init__(self, path: str | Path):
        db_path = str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        if db_path != ":memory:":
            Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS skill_outcomes (
                       skill_id TEXT NOT NULL,
                       idempotency_key TEXT NOT NULL,
                       status TEXT NOT NULL,
                       payload_json TEXT NOT NULL,
                       completed_at TEXT,
                       PRIMARY KEY (skill_id, idempotency_key)
                   )"""
            )

    def save(self, outcome: ExecutionOutcome) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO skill_outcomes (
                       idempotency_key, skill_id, status, payload_json, completed_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    outcome.idempotency_key,
                    outcome.skill_id,
                    outcome.status.value,
                    outcome.model_dump_json(),
                    outcome.completed_at.isoformat() if outcome.completed_at else None,
                ),
            )

    def get(self, skill_id: str, idempotency_key: str) -> ExecutionOutcome | None:
        with self._lock:
            row = self._db.execute(
                """SELECT payload_json FROM skill_outcomes
                   WHERE skill_id = ? AND idempotency_key = ?""",
                (skill_id, idempotency_key),
            ).fetchone()
        return (
            ExecutionOutcome.model_validate_json(row["payload_json"]) if row else None
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()
