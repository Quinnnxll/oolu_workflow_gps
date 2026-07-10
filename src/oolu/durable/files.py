"""User files: named, editable documents living in the durable database.

The conversation produces and consumes documents and sheets; they need a
home the user can open, read, and edit from the app — on the same durable
connection the runs live on (SQLite locally, Postgres on a host), so a
hosted deployment's files are as multi-device as its runs.

Deliberately text-only and small: documents and CSV sheets, not an object
store. Large binary evidence stays in the content-addressed artifact
store; these are the files a person edits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

# A person-editable file, not a data lake: keep rows sane.
MAX_FILE_BYTES = 1_000_000


class FileTooLargeError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


MAX_FOLDER_CHARS = 200


def normalize_folder(folder: object) -> str:
    """A folder path in canonical form: '/'-separated segments, no blank
    segments, no leading/trailing slashes, bounded length. '' = the root."""
    text = str(folder or "").strip()
    segments = [s.strip() for s in text.split("/") if s.strip()]
    normalized = "/".join(segments)
    if len(normalized) > MAX_FOLDER_CHARS:
        raise ValueError(f"folder path exceeds {MAX_FOLDER_CHARS} characters")
    return normalized


class UserFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    file_id: str = Field(default_factory=lambda: uuid4().hex)
    tenant_id: str
    # A file lives with its owner: None = the Life environment's shared
    # drawer; a node id = that node's own independent files in Work.
    node_id: str | None = None
    name: str
    # Where the file sits INSIDE its drawer: a '/'-separated folder path
    # ('' = the drawer's root). Folders are derived from the files that
    # name them — organization, not a separate object.
    folder: str = ""
    media_type: str = "text/markdown"
    content: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def size(self) -> int:
        return len(self.content.encode("utf-8"))


_SCHEMA = """CREATE TABLE IF NOT EXISTS user_files (
    file_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""


class UserFileStore:
    """Tenant-scoped file CRUD over the shell's own durable connection."""

    def __init__(self, conn) -> None:
        self._conn = conn
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    def save(self, file: UserFile) -> UserFile:
        if file.size > MAX_FILE_BYTES:
            raise FileTooLargeError(
                f"file exceeds {MAX_FILE_BYTES} bytes; large data belongs in"
                " the artifact store"
            )
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO user_files (file_id, tenant_id, payload_json)
                   VALUES (?, ?, ?)
                   ON CONFLICT(file_id) DO UPDATE SET
                     payload_json = excluded.payload_json""",
                (file.file_id, file.tenant_id, file.model_dump_json()),
            )
        return file

    def get(self, file_id: str, *, tenant: str) -> UserFile | None:
        with self._conn.lock:
            row = self._conn.db.execute(
                "SELECT payload_json FROM user_files"
                " WHERE file_id = ? AND tenant_id = ?",
                (file_id, tenant),
            ).fetchone()
        return UserFile.model_validate_json(row["payload_json"]) if row else None

    def list(self, *, tenant: str, node_id: str | None = None) -> list[UserFile]:
        """One drawer at a time: the Life files (node_id None) or one
        node's own files — never a mixed listing."""
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM user_files WHERE tenant_id = ?"
                " ORDER BY rowid ASC",
                (tenant,),
            ).fetchall()
        files = [UserFile.model_validate_json(r["payload_json"]) for r in rows]
        return [f for f in files if f.node_id == node_id]

    def delete(self, file_id: str, *, tenant: str) -> bool:
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM user_files WHERE file_id = ? AND tenant_id = ?",
                (file_id, tenant),
            )
            return cursor.rowcount > 0
