from __future__ import annotations

import builtins
import hashlib
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..persistence import Migration, migrate
from .models import ReusableSkill

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {"a", "an", "the", "to", "of", "and", "or", "for", "with", "in", "on", "my", "this"}
)
_FIELD_WEIGHTS = {"name": 3, "tags": 3, "keywords": 2, "summary": 1}


def _semver_tuple(semver: str) -> tuple[int, int, int]:
    parts = (semver.split("-", 1)[0].split("."))[:3]
    nums = [int(p) if p.isdigit() else 0 for p in parts]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


# Excluded from the content hash: the hash covers *behaviour* (signature, params,
# actions, constraints), not provenance/telemetry — so the same skill learned from
# a different demonstration instance is the same content, and re-learning is a no-op.
_VOLATILE_FIELDS = (
    "created_at",
    "updated_at",
    "success_count",
    "failure_count",
    "demonstration_ids",
)
_VOLATILE_ACTION_FIELDS = ("id", "observed_at")
_ACTION_LISTS = ("actions", "recovery_actions")


def _content_hash(skill: ReusableSkill) -> str:
    data = skill.model_dump(mode="json")
    for field in _VOLATILE_FIELDS:
        data.pop(field, None)
    for key in _ACTION_LISTS:
        for action in data.get(key, []):
            for field in _VOLATILE_ACTION_FIELDS:
                action.pop(field, None)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RegisteredSkill(BaseModel):
    model_config = ConfigDict(frozen=True)

    skill_id: str
    semver: str
    content_hash: str
    name: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    skill: ReusableSkill
    created_at: datetime


class ScoredSkill(BaseModel):
    model_config = ConfigDict(frozen=True)

    score: float
    skill: RegisteredSkill


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS skill_registry (
               skill_id TEXT NOT NULL,
               semver TEXT NOT NULL,
               v_major INTEGER NOT NULL,
               v_minor INTEGER NOT NULL,
               v_patch INTEGER NOT NULL,
               content_hash TEXT NOT NULL,
               name TEXT NOT NULL,
               summary TEXT NOT NULL,
               tags_json TEXT NOT NULL,
               keywords TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               created_at TEXT NOT NULL,
               PRIMARY KEY (skill_id, semver)
           )"""
    )


def _drop(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS skill_registry")


SKILL_REGISTRY_MIGRATIONS: tuple[Migration, ...] = (Migration(up=_create, down=_drop),)


class SkillRegistry:
    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        location = Path(path).expanduser()
        if str(location) != ":memory:":
            # First run on a fresh machine: the data directory may not
            # exist yet, and sqlite will not create it for us.
            location.resolve().parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(location), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            migrate(self._db, SKILL_REGISTRY_MIGRATIONS, label="skill-registry")

    def register(
        self,
        skill: ReusableSkill,
        *,
        semver: str,
        summary: str | None = None,
        tags: list[str] | None = None,
    ) -> RegisteredSkill:
        summary = summary or skill.description
        tags = list(tags or [])
        digest = _content_hash(skill)
        keywords = sorted(
            set(_tokens(skill.name) + _tokens(summary) + _tokens(" ".join(tags)))
        )
        created = datetime.now(UTC)
        major, minor, patch = _semver_tuple(semver)
        with self._lock:
            existing = self._db.execute(
                "SELECT content_hash FROM skill_registry WHERE skill_id = ? AND semver = ?",
                (skill.id, semver),
            ).fetchone()
            if existing is not None and existing["content_hash"] != digest:
                raise ValueError(
                    f"{skill.id}@{semver} already registered with different content"
                )
            self._db.execute(
                """INSERT OR REPLACE INTO skill_registry
                   (skill_id, semver, v_major, v_minor, v_patch, content_hash, name,
                    summary, tags_json, keywords, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    skill.id,
                    semver,
                    major,
                    minor,
                    patch,
                    digest,
                    skill.name,
                    summary,
                    json.dumps(tags),
                    " ".join(keywords),
                    skill.model_dump_json(),
                    created.isoformat(),
                ),
            )
            self._db.commit()
        return RegisteredSkill(
            skill_id=skill.id,
            semver=semver,
            content_hash=digest,
            name=skill.name,
            summary=summary,
            tags=tags,
            keywords=keywords,
            skill=skill,
            created_at=created,
        )

    def versions(self, skill_id: str) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                """SELECT semver FROM skill_registry WHERE skill_id = ?
                   ORDER BY v_major DESC, v_minor DESC, v_patch DESC""",
                (skill_id,),
            ).fetchall()
        return [row["semver"] for row in rows]

    def get(
        self, skill_id: str, *, semver: str | None = None
    ) -> RegisteredSkill | None:
        with self._lock:
            if semver is None:
                row = self._db.execute(
                    """SELECT * FROM skill_registry WHERE skill_id = ?
                       ORDER BY v_major DESC, v_minor DESC, v_patch DESC LIMIT 1""",
                    (skill_id,),
                ).fetchone()
            else:
                row = self._db.execute(
                    "SELECT * FROM skill_registry WHERE skill_id = ? AND semver = ?",
                    (skill_id, semver),
                ).fetchone()
        return self._row(row) if row is not None else None

    def list(self, *, limit: int = 100) -> builtins.list[RegisteredSkill]:
        return [scored.skill for scored in self._latest(limit=limit)]

    def search(self, query: str, *, limit: int = 8) -> builtins.list[ScoredSkill]:
        terms = set(_tokens(query))
        if not terms:
            return self._latest(limit=limit)
        scored: list[ScoredSkill] = []
        for candidate in self._latest(limit=10_000):
            score = self._score(candidate.skill, terms)
            if score > 0:
                scored.append(ScoredSkill(score=score, skill=candidate.skill))
        scored.sort(key=lambda item: (item.score, item.skill.created_at), reverse=True)
        return scored[:limit]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _latest(self, *, limit: int) -> builtins.list[ScoredSkill]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM skill_registry").fetchall()
        latest: dict[str, sqlite3.Row] = {}
        for row in rows:
            current = latest.get(row["skill_id"])
            key = (row["v_major"], row["v_minor"], row["v_patch"])
            if current is None or key > (
                current["v_major"],
                current["v_minor"],
                current["v_patch"],
            ):
                latest[row["skill_id"]] = row
        skills = sorted(
            (self._row(row) for row in latest.values()),
            key=lambda item: item.created_at,
            reverse=True,
        )
        return [ScoredSkill(score=0.0, skill=skill) for skill in skills[:limit]]

    @staticmethod
    def _score(skill: RegisteredSkill, terms: set[str]) -> float:
        fields = {
            "name": set(_tokens(skill.name)),
            "tags": set(_tokens(" ".join(skill.tags))),
            "keywords": set(skill.keywords),
            "summary": set(_tokens(skill.summary)),
        }
        return float(
            sum(
                weight * len(terms & fields[field])
                for field, weight in _FIELD_WEIGHTS.items()
            )
        )

    @staticmethod
    def _row(row: sqlite3.Row) -> RegisteredSkill:
        return RegisteredSkill(
            skill_id=row["skill_id"],
            semver=row["semver"],
            content_hash=row["content_hash"],
            name=row["name"],
            summary=row["summary"],
            tags=json.loads(row["tags_json"]),
            keywords=row["keywords"].split() if row["keywords"] else [],
            skill=ReusableSkill.model_validate_json(row["payload_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
