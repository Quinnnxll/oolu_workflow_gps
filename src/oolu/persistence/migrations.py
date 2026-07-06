"""A small, dependency-free SQLite migration runner shared by every local store.

Every persisted SQLite schema in OoLu is versioned through the built-in
``PRAGMA user_version`` counter rather than an ad-hoc meta table. A schema is
described as an ordered list of :class:`Migration` steps; step ``i`` (0-indexed)
upgrades the database from ``user_version == i`` to ``user_version == i + 1``.

The runner is deliberately tiny and synchronous:

* Forward migration is the default — opening a store brings it up to the latest
  version the running code understands.
* A database whose ``user_version`` is *newer* than the code supports is refused
  rather than silently corrupted (forward-compatibility guard).
* Each step may declare a reversible ``down`` so rollback is testable. Rolling
  back past an irreversible step raises rather than guessing.

Migration steps should be idempotent at the SQL level where practical (e.g.
``CREATE TABLE IF NOT EXISTS``) so a pre-existing, unversioned database created
by older code is adopted cleanly as version 1.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

MigrationStep = Callable[[sqlite3.Connection], None]


class SchemaError(RuntimeError):
    """Raised when a database cannot be migrated to the requested version."""


@dataclass(frozen=True)
class Migration:
    """A single forward step with an optional reverse step.

    ``up`` and ``down`` receive an open connection and must only issue DDL/DML;
    the runner owns version bookkeeping and the surrounding transaction commit.
    """

    up: MigrationStep
    down: MigrationStep | None = None


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the current ``PRAGMA user_version`` for the connection."""

    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA does not accept bound parameters; the value is always a trusted int.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def migrate(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration],
    *,
    label: str,
    target: int | None = None,
) -> int:
    """Migrate ``conn`` to ``target`` (default: the latest known version).

    Returns the resulting ``user_version``. Forward steps run their ``up``;
    reverse steps run their ``down``. Raises :class:`SchemaError` when the
    database is newer than supported, when ``target`` is out of range, or when a
    rollback crosses an irreversible step.
    """

    latest = len(migrations)
    if target is None:
        target = latest
    if not 0 <= target <= latest:
        raise SchemaError(
            f"{label}: target version {target} is outside the supported range 0..{latest}"
        )

    current = schema_version(conn)
    if current > latest:
        raise SchemaError(
            f"{label}: database schema version {current} is newer than supported {latest}"
        )

    while current < target:
        migrations[current].up(conn)
        current += 1
        _set_version(conn, current)

    while current > target:
        step = migrations[current - 1]
        if step.down is None:
            raise SchemaError(
                f"{label}: migration {current} is irreversible; cannot roll back"
            )
        step.down(conn)
        current -= 1
        _set_version(conn, current)

    conn.commit()
    return current
