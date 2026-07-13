"""User files: named documents living in the durable database.

The conversation produces and consumes documents and sheets; they need a
home the user can open, read, and edit from the app — on the same durable
connection the runs live on (SQLite locally, Postgres on a host), so a
hosted deployment's files are as multi-device as its runs.

Two shapes, one drawer. INLINE files (documents, sheets, small images as
data URLs) live in the row itself, person-editable, capped sane. BLOB
files — the PDFs, decks, videos, and datasets developers and creators
actually exchange — keep their bytes in the content-addressed artifact
store next door, with the row carrying only metadata and the reference:
the drawer stays a drawer, and the database never swallows a video.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

# A person-editable INLINE file, not a data lake: keep rows sane.
MAX_FILE_BYTES = 1_000_000
# The blob door's own ceiling — filesystem-backed, so generous, but still
# a stated number the refusal can speak.
MAX_BLOB_BYTES = 100_000_000


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
    # A BLOB file: bytes live in the artifact store under this reference
    # (sha256:...), the row carries only metadata. content stays "".
    blob_ref: str | None = None
    blob_size: int = 0
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def size(self) -> int:
        if self.blob_ref:
            return self.blob_size
        return len(self.content.encode("utf-8"))


_SCHEMA = """CREATE TABLE IF NOT EXISTS user_files (
    file_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
)"""


class UserFileStore:
    """Tenant-scoped file CRUD over the shell's own durable connection.

    ``artifacts`` (a ``FilesystemArtifactStore``) opens the blob door:
    ``save_bytes``/``read_bytes`` for files whose bytes do not belong in
    a database row. Without it the store stays exactly what it was —
    inline documents only.
    """

    def __init__(self, conn, *, artifacts=None) -> None:
        self._conn = conn
        self._artifacts = artifacts
        with self._conn.transaction() as db:
            db.execute(_SCHEMA)

    @property
    def blobs_enabled(self) -> bool:
        return self._artifacts is not None

    def save(self, file: UserFile) -> UserFile:
        if not file.blob_ref and file.size > MAX_FILE_BYTES:
            raise FileTooLargeError(
                f"file exceeds {MAX_FILE_BYTES} bytes; large data belongs in"
                " the artifact store"
            )
        self._persist(file)
        return file

    def save_bytes(self, file: UserFile, data: bytes) -> UserFile:
        """The blob door: the bytes land in the artifact store, the row
        keeps the metadata and the self-verifying reference."""
        if self._artifacts is None:
            raise FileTooLargeError(
                "this host keeps no blob store — only inline documents fit"
            )
        if len(data) > MAX_BLOB_BYTES:
            raise FileTooLargeError(
                f"file exceeds {MAX_BLOB_BYTES} bytes"
            )
        ref = self._artifacts.put(
            file.file_id, data, media_type=file.media_type
        )
        saved = file.model_copy(
            update={"content": "", "blob_ref": ref, "blob_size": len(data)}
        )
        self._persist(saved)
        return saved

    def read_bytes(self, file: UserFile) -> bytes:
        """The file's true bytes, whichever shape it is stored in."""
        if file.blob_ref:
            if self._artifacts is None:
                raise FileTooLargeError(
                    "this host keeps no blob store — the bytes are elsewhere"
                )
            return self._artifacts.get(file.blob_ref)
        return file.content.encode("utf-8")

    def _persist(self, file: UserFile) -> None:
        with self._conn.transaction() as db:
            db.execute(
                """INSERT INTO user_files (file_id, tenant_id, payload_json)
                   VALUES (?, ?, ?)
                   ON CONFLICT(file_id) DO UPDATE SET
                     payload_json = excluded.payload_json""",
                (file.file_id, file.tenant_id, file.model_dump_json()),
            )

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
                "SELECT payload_json FROM user_files WHERE tenant_id = ?",
                (tenant,),
            ).fetchall()
        files = [UserFile.model_validate_json(r["payload_json"]) for r in rows]
        # Oldest-first by the record's own clock — rowid is SQLite-only
        # and would be an UndefinedColumn on the PostgreSQL backend.
        files.sort(key=lambda f: (f.created_at.isoformat(), f.file_id))
        return [f for f in files if f.node_id == node_id]

    def delete(self, file_id: str, *, tenant: str) -> bool:
        doomed = self.get(file_id, tenant=tenant)
        with self._conn.transaction() as db:
            cursor = db.execute(
                "DELETE FROM user_files WHERE file_id = ? AND tenant_id = ?",
                (file_id, tenant),
            )
            deleted = cursor.rowcount > 0
        # The blob goes too — UNLESS another row still references the same
        # content-addressed bytes (identical uploads deduplicate).
        if (
            deleted
            and doomed is not None
            and doomed.blob_ref
            and self._artifacts is not None
            and not self._blob_in_use(doomed.blob_ref)
        ):
            self._artifacts.delete(doomed.blob_ref)
        return deleted

    def _blob_in_use(self, blob_ref: str) -> bool:
        with self._conn.lock:
            rows = self._conn.db.execute(
                "SELECT payload_json FROM user_files"
            ).fetchall()
        return any(
            UserFile.model_validate_json(row["payload_json"]).blob_ref == blob_ref
            for row in rows
        )
