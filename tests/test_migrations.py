"""Schema-versioning and forward/rollback migration tests for every SQLite store.

These exercise the shared ``persistence.migrate`` runner directly and through
each concrete local store, so a regression in any store's schema history is
caught here rather than in production.
"""

from __future__ import annotations

import sqlite3

import pytest

from oolu.cache.store import MIGRATIONS as CACHE_MIGRATIONS
from oolu.cache.store import LocalScriptCache
from oolu.durable.connection import DURABLE_MIGRATIONS
from oolu.identity.store import IDENTITY_MIGRATIONS
from oolu.knowledge.client import KNOWLEDGE_MIGRATIONS, LocalKnowledgeClient
from oolu.knowledge.remote import QUARANTINE_MIGRATIONS
from oolu.orchestrator.store import RUN_STATE_MIGRATIONS
from oolu.persistence import Migration, SchemaError, migrate, schema_version
from oolu.replies.learned import MIGRATIONS as REPLY_MIGRATIONS
from oolu.replies.learned import LocalLearnedReplyStore
from oolu.skills.store import (
    SKILL_MIGRATIONS,
    LocalExecutionStore,
    LocalSkillStore,
)
from oolu.worker.ledger import WORKER_MIGRATIONS

# Every versioned schema in the project, paired with its migration history. New
# stores added later must be registered here so the shared invariants below cover
# them automatically.
ALL_SCHEMAS = [
    pytest.param(CACHE_MIGRATIONS, "script_cache", id="script_cache"),
    pytest.param(REPLY_MIGRATIONS, "learned_replies", id="learned_replies"),
    pytest.param(KNOWLEDGE_MIGRATIONS, "knowledge", id="knowledge"),
    pytest.param(QUARANTINE_MIGRATIONS, "crowd_quarantine", id="crowd_quarantine"),
    pytest.param(SKILL_MIGRATIONS, "skills", id="skills"),
    pytest.param(RUN_STATE_MIGRATIONS, "workflow_runs", id="workflow_runs"),
    pytest.param(DURABLE_MIGRATIONS, "durable", id="durable"),
    pytest.param(IDENTITY_MIGRATIONS, "identity", id="identity"),
    pytest.param(WORKER_MIGRATIONS, "worker", id="worker"),
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row[0] for row in rows}


# --------------------------------------------------------------------------- #
# Shared invariants across every registered schema.                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("migrations, label", ALL_SCHEMAS)
def test_fresh_database_migrates_to_latest(migrations, label):
    conn = _connect()
    assert schema_version(conn) == 0
    final = migrate(conn, migrations, label=label)
    assert final == len(migrations)
    assert schema_version(conn) == len(migrations)
    assert _table_names(conn), "expected at least one table after migration"


@pytest.mark.parametrize("migrations, label", ALL_SCHEMAS)
def test_migrate_is_idempotent(migrations, label):
    conn = _connect()
    migrate(conn, migrations, label=label)
    before = _table_names(conn)
    # Running again must be a no-op and must not error on existing tables.
    migrate(conn, migrations, label=label)
    assert schema_version(conn) == len(migrations)
    assert _table_names(conn) == before


@pytest.mark.parametrize("migrations, label", ALL_SCHEMAS)
def test_full_rollback_then_forward_round_trip(migrations, label):
    conn = _connect()
    migrate(conn, migrations, label=label)
    tables_at_latest = _table_names(conn)

    # Roll all the way back to an empty database.
    migrate(conn, migrations, label=label, target=0)
    assert schema_version(conn) == 0

    # And forward again to the latest version — schema must be reconstructed.
    migrate(conn, migrations, label=label)
    assert schema_version(conn) == len(migrations)
    assert _table_names(conn) == tables_at_latest


@pytest.mark.parametrize("migrations, label", ALL_SCHEMAS)
def test_refuses_database_newer_than_supported(migrations, label):
    conn = _connect()
    migrate(conn, migrations, label=label)
    # Simulate a database written by a newer release.
    conn.execute(f"PRAGMA user_version = {len(migrations) + 1}")
    conn.commit()
    with pytest.raises(SchemaError):
        migrate(conn, migrations, label=label)


@pytest.mark.parametrize("migrations, label", ALL_SCHEMAS)
def test_target_out_of_range_is_rejected(migrations, label):
    conn = _connect()
    with pytest.raises(SchemaError):
        migrate(conn, migrations, label=label, target=len(migrations) + 5)


# --------------------------------------------------------------------------- #
# Runner-level behavior independent of any specific store.                     #
# --------------------------------------------------------------------------- #
def test_stepwise_forward_and_backward():
    log: list[str] = []
    migrations = (
        Migration(
            up=lambda c: (c.execute("CREATE TABLE a (x)"), log.append("up1")),
            down=lambda c: (c.execute("DROP TABLE a"), log.append("down1")),
        ),
        Migration(
            up=lambda c: (c.execute("CREATE TABLE b (x)"), log.append("up2")),
            down=lambda c: (c.execute("DROP TABLE b"), log.append("down2")),
        ),
    )
    conn = _connect()

    migrate(conn, migrations, label="demo", target=1)
    assert schema_version(conn) == 1
    assert "a" in _table_names(conn) and "b" not in _table_names(conn)

    migrate(conn, migrations, label="demo")  # forward to 2
    assert {"a", "b"} <= _table_names(conn)

    migrate(conn, migrations, label="demo", target=0)  # back to empty
    assert schema_version(conn) == 0
    assert "a" not in _table_names(conn) and "b" not in _table_names(conn)
    assert log == ["up1", "up2", "down2", "down1"]


def test_irreversible_rollback_raises():
    migrations = (Migration(up=lambda c: c.execute("CREATE TABLE a (x)"), down=None),)
    conn = _connect()
    migrate(conn, migrations, label="demo")
    with pytest.raises(SchemaError):
        migrate(conn, migrations, label="demo", target=0)


# --------------------------------------------------------------------------- #
# A pre-existing, unversioned database is adopted (forward-compatible upgrade).#
# --------------------------------------------------------------------------- #
def test_legacy_unversioned_cache_is_adopted(tmp_path):
    db = tmp_path / "legacy-cache.db"
    raw = sqlite3.connect(db)
    raw.execute(
        """CREATE TABLE script_cache (
               cache_key TEXT PRIMARY KEY, script TEXT NOT NULL,
               dependencies TEXT NOT NULL, tier TEXT NOT NULL, model TEXT NOT NULL,
               success_count INTEGER NOT NULL DEFAULT 0,
               failure_count INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    raw.commit()
    raw.close()
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 0

    # Opening through the store must adopt the legacy file as version 1 without
    # losing its existing table.
    cache = LocalScriptCache(db)
    cache.store_success("k", script="print(1)", dependencies=[], tier="fast", model="m")
    assert cache.get("k") is not None
    cache.close()
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# Stores survive a real close/reopen cycle on a versioned file.               #
# --------------------------------------------------------------------------- #
def test_stores_persist_across_reopen(tmp_path):
    cache = LocalScriptCache(tmp_path / "c.db")
    cache.store_success("k", script="x", dependencies=["a"], tier="fast", model="m")
    cache.close()
    assert LocalScriptCache(tmp_path / "c.db").get("k") is not None

    replies = LocalLearnedReplyStore(tmp_path / "r.db")
    replies.teach(scope="s", prompt="hi", reply="hello")
    replies.close()
    reopened = LocalLearnedReplyStore(tmp_path / "r.db")
    assert schema_version(sqlite3.connect(tmp_path / "r.db")) == len(REPLY_MIGRATIONS)
    reopened.close()

    knowledge = LocalKnowledgeClient(tmp_path / "k.db")
    knowledge.record_dependency_success("cv2", "opencv-python")
    knowledge.close()
    assert LocalKnowledgeClient(tmp_path / "k.db").get_dependency_hints("cv2")


def test_skill_db_shares_one_version_for_both_stores(tmp_path):
    # The catalog and the idempotency ledger live in one file and must not fight
    # over user_version.
    db = tmp_path / "skill.db"
    skills = LocalSkillStore(db)
    ledger = LocalExecutionStore(db)
    tables = _table_names(sqlite3.connect(db))
    assert {"skills", "skill_outcomes"} <= tables
    assert schema_version(sqlite3.connect(db)) == len(SKILL_MIGRATIONS)
    skills.close()
    ledger.close()
