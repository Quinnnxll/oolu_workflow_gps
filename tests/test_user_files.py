"""User files: documents and sheets living in the durable database.

The conversation's files, tenant-scoped, editable through /v1/files —
SQLite locally, Postgres on a host, the same durable connection the runs
use.
"""

from __future__ import annotations

import pytest
from test_http_gateway import _app, _req

from oolu.durable import DurableConnection, FileTooLargeError, UserFile, UserFileStore
from oolu.durable.files import MAX_FILE_BYTES
from oolu.gateway import GatewayApp


# --------------------------------------------------------------------------- #
# The store.                                                                   #
# --------------------------------------------------------------------------- #
def test_store_roundtrip_and_tenant_scoping(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    try:
        store = UserFileStore(conn)
        doc = store.save(
            UserFile(tenant_id="t1", name="notes.md", content="# hello")
        )
        assert store.get(doc.file_id, tenant="t1").content == "# hello"
        # Another tenant cannot see or delete it.
        assert store.get(doc.file_id, tenant="t2") is None
        assert store.delete(doc.file_id, tenant="t2") is False
        assert [f.name for f in store.list(tenant="t1")] == ["notes.md"]
        assert store.list(tenant="t2") == []
        assert store.delete(doc.file_id, tenant="t1") is True
        assert store.list(tenant="t1") == []
    finally:
        conn.close()


def test_node_files_live_in_their_own_drawer(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    try:
        store = UserFileStore(conn)
        store.save(UserFile(tenant_id="t1", name="life.md"))
        store.save(UserFile(tenant_id="t1", node_id="n1", name="node-log.csv"))
        store.save(UserFile(tenant_id="t1", node_id="n2", name="other.md"))

        # One drawer at a time — never a mixed listing.
        assert [f.name for f in store.list(tenant="t1")] == ["life.md"]
        assert [f.name for f in store.list(tenant="t1", node_id="n1")] == [
            "node-log.csv"
        ]
        assert [f.name for f in store.list(tenant="t1", node_id="n2")] == ["other.md"]
    finally:
        conn.close()


def test_store_refuses_oversized_files(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    try:
        store = UserFileStore(conn)
        with pytest.raises(FileTooLargeError):
            store.save(
                UserFile(tenant_id="t1", name="big.md", content="x" * (MAX_FILE_BYTES + 1))
            )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The routes.                                                                  #
# --------------------------------------------------------------------------- #
def _files_app(tmp_path):
    base, conn, ident = _app(tmp_path)
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        files=UserFileStore(conn),
    )
    return app, conn, ident


def test_files_crud_end_to_end(tmp_path):
    app, conn, ident = _files_app(tmp_path)
    try:
        token = ident.token("user-1", "t1")

        created = app.handle(
            _req(
                "POST",
                "/v1/files",
                token=token,
                body={"name": "budget.csv", "content": "item,cost\ncoffee,3"},
            )
        )
        assert created.status == 201
        file_id = created.body["file_id"]
        assert created.body["media_type"] == "text/csv"  # inferred from name

        listed = app.handle(_req("GET", "/v1/files", token=token))
        assert [f["name"] for f in listed.body["items"]] == ["budget.csv"]
        assert "content" not in listed.body["items"][0]  # listing is metadata

        read = app.handle(_req("GET", f"/v1/files/{file_id}", token=token))
        assert read.body["content"] == "item,cost\ncoffee,3"

        edited = app.handle(
            _req(
                "PUT",
                f"/v1/files/{file_id}",
                token=token,
                body={"content": "item,cost\ncoffee,3\ntea,2", "name": "budget-q3.csv"},
            )
        )
        assert edited.status == 200
        assert edited.body["name"] == "budget-q3.csv"
        assert "tea,2" in edited.body["content"]

        # Another tenant gets a 404, never a peek.
        other = app.handle(
            _req("GET", f"/v1/files/{file_id}", token=ident.token("user-2", "t2"))
        )
        assert other.status == 404

        # A node's files ride the node_id query, separate from the Life drawer.
        node_file = app.handle(
            _req(
                "POST",
                "/v1/files",
                token=token,
                body={"name": "n1-report.md", "node_id": "n1", "content": "x"},
            )
        )
        assert node_file.status == 201
        life_only = app.handle(_req("GET", "/v1/files", token=token))
        assert [f["name"] for f in life_only.body["items"]] == ["budget-q3.csv"]
        node_only = app.handle(
            _req("GET", "/v1/files", token=token, query={"node_id": "n1"})
        )
        assert [f["name"] for f in node_only.body["items"]] == ["n1-report.md"]

        gone = app.handle(_req("DELETE", f"/v1/files/{file_id}", token=token))
        assert gone.status == 200
        assert (
            app.handle(_req("GET", f"/v1/files/{file_id}", token=token)).status == 404
        )
    finally:
        conn.close()


def test_files_require_auth_name_and_a_store(tmp_path):
    app, conn, ident = _files_app(tmp_path)
    try:
        assert app.handle(_req("GET", "/v1/files")).status == 401
        bad = app.handle(
            _req("POST", "/v1/files", token=ident.token("u", "t1"), body={})
        )
        assert bad.status == 400
    finally:
        conn.close()

    plain, conn2, ident2 = _app(tmp_path / "b")  # no file store configured
    try:
        resp = plain.handle(_req("GET", "/v1/files", token=ident2.token("u", "t1")))
        assert resp.status == 404
    finally:
        conn2.close()


def test_folders_organize_a_drawer(tmp_path):
    app, conn, ident = _files_app(tmp_path)
    try:
        token = ident.token("user-1", "t1")

        # A file is created INSIDE a folder; the path is normalized.
        created = app.handle(
            _req(
                "POST",
                "/v1/files",
                token=token,
                body={
                    "name": "q3.md",
                    "content": "# Q3",
                    "folder": " /reports/2026/ ",
                },
            )
        )
        assert created.status == 201, created.body
        assert created.body["folder"] == "reports/2026"
        file_id = created.body["file_id"]

        # The listing carries the folder so a client can navigate.
        listed = app.handle(_req("GET", "/v1/files", token=token))
        assert listed.body["items"][0]["folder"] == "reports/2026"

        # Moving a file is just updating its folder; content is untouched.
        moved = app.handle(
            _req(
                "PUT",
                f"/v1/files/{file_id}",
                token=token,
                body={"folder": "archive"},
            )
        )
        assert moved.status == 200
        assert moved.body["folder"] == "archive"
        assert moved.body["content"] == "# Q3"

        # A root file simply has no folder.
        root = app.handle(
            _req("POST", "/v1/files", token=token, body={"name": "note.md"})
        )
        assert root.body["folder"] == ""

        # A silly-long path is refused, never truncated silently.
        refused = app.handle(
            _req(
                "POST",
                "/v1/files",
                token=token,
                body={"name": "x.md", "folder": "a/" * 200},
            )
        )
        assert refused.status == 400
    finally:
        conn.close()


def test_the_drawer_speaks_real_file_types():
    """The formats developers, creators, and engineers exchange are typed
    honestly by extension — so viewers, players, and the download door
    all know what they are holding."""
    from oolu.gateway.app import _media_type_for

    assert _media_type_for("paper.PDF") == "application/pdf"
    assert _media_type_for("report.docx") == (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    )
    assert _media_type_for("books.XLSX") == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert _media_type_for("deck.pptx") == (
        "application/vnd.openxmlformats-officedocument"
        ".presentationml.presentation"
    )
    assert _media_type_for("shot.JPG") == "image/jpeg"
    assert _media_type_for("shot.jpeg") == "image/jpeg"
    assert _media_type_for("logo.png") == "image/png"
    assert _media_type_for("loop.gif") == "image/gif"
    assert _media_type_for("clip.mp4") == "video/mp4"
    assert _media_type_for("song.MP3") == "audio/mpeg"
    # The old floor still stands.
    assert _media_type_for("rows.csv") == "text/csv"
    assert _media_type_for("notes") == "text/markdown"


# --------------------------------------------------------------------------- #
# The blob door: past the inline cap, bytes live in the artifact store.        #
# --------------------------------------------------------------------------- #
def test_the_blob_door_end_to_end(tmp_path):
    """Raw bytes in (no base64, no JSON envelope), raw bytes out — a 2 MiB
    video lands past the inline cap, streams back byte-identical, refuses
    text edits, stays tenant-walled, and leaves no orphan on delete."""
    from test_http_gateway import NOW

    from oolu.durable.artifacts import FilesystemArtifactStore
    from oolu.gateway.http import Request

    base, conn, ident = _app(tmp_path)
    store = UserFileStore(
        conn, artifacts=FilesystemArtifactStore(tmp_path / "blobs")
    )
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        files=store,
    )
    token = ident.token("user-1", "t1")
    try:
        payload = bytes(range(256)) * 8192  # 2 MiB — past the inline cap
        uploaded = app.handle(
            Request(
                method="POST",
                path="/v1/files/upload",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "video/mp4",
                },
                query={"name": "clip.mp4", "folder": "footage"},
                raw=payload,
                now=NOW,
            )
        )
        assert uploaded.status == 201, uploaded.body
        meta = uploaded.body
        assert meta["has_blob"] is True
        assert meta["size"] == len(payload)
        assert meta["media_type"] == "video/mp4"
        assert meta["folder"] == "footage"
        file_id = meta["file_id"]

        # The row never swallowed the bytes; /content returns them whole.
        read = app.handle(_req("GET", f"/v1/files/{file_id}", token=token))
        assert read.body["content"] == "" and read.body["has_blob"] is True
        content = app.handle(
            _req("GET", f"/v1/files/{file_id}/content", token=token)
        )
        assert content.status == 200
        assert content.body == payload
        assert content.content_type == "video/mp4"
        assert 'filename="clip.mp4"' in content.headers["Content-Disposition"]

        # Text-editing a binary is refused in words; renaming still works.
        refused = app.handle(
            _req(
                "PUT",
                f"/v1/files/{file_id}",
                token=token,
                body={"content": "oops"},
            )
        )
        assert refused.status == 400
        renamed = app.handle(
            _req(
                "PUT",
                f"/v1/files/{file_id}",
                token=token,
                body={"name": "match.mp4"},
            )
        )
        assert renamed.status == 200, renamed.body

        # Another tenant gets a 404 on the bytes too, never a peek.
        other = app.handle(
            _req(
                "GET",
                f"/v1/files/{file_id}/content",
                token=ident.token("user-2", "t2"),
            )
        )
        assert other.status == 404

        # Deleting the file deletes the blob from disk — no orphans.
        def blobs():
            return [
                p
                for p in (tmp_path / "blobs").rglob("*")
                if p.is_file() and p.suffix != ".meta"
            ]

        assert len(blobs()) == 1
        app.handle(_req("DELETE", f"/v1/files/{file_id}", token=token))
        assert blobs() == []
    finally:
        conn.close()


def test_identical_blobs_deduplicate_and_delete_safely(tmp_path):
    from oolu.durable.artifacts import FilesystemArtifactStore

    conn = DurableConnection(tmp_path / "d.db")
    try:
        store = UserFileStore(
            conn, artifacts=FilesystemArtifactStore(tmp_path / "blobs")
        )
        data = b"same bytes" * 1000
        first = store.save_bytes(
            UserFile(
                tenant_id="t1", name="a.bin", media_type="application/octet-stream"
            ),
            data,
        )
        second = store.save_bytes(
            UserFile(
                tenant_id="t1", name="b.bin", media_type="application/octet-stream"
            ),
            data,
        )
        assert first.blob_ref == second.blob_ref  # content addressing dedupes
        # Deleting one keeps the shared bytes alive for the other...
        assert store.delete(first.file_id, tenant="t1") is True
        assert store.read_bytes(store.get(second.file_id, tenant="t1")) == data
        # ...and deleting the LAST reference removes the blob itself.
        store.delete(second.file_id, tenant="t1")
        assert [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()] == []
    finally:
        conn.close()
