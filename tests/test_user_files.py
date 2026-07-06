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
