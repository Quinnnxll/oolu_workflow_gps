"""Ship-and-operate: the data-subject's rights, the legal surface, and
the operator's numbers.

Exit gate: /v1/account/export returns everything the host holds about
the caller as one JSON document; /v1/account/delete demands the password
(a stolen session must not destroy an account), erases the per-person
stores, disables the account forever, and says exactly what it did and
did not remove; the legal URLs are public — operator files verbatim,
unmissable templates until then; and /v1/metrics is the operator's
(permission-gated), carrying uptime.
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_http_gateway import _app, _req

from oolu.billing import FakeCardVault, PaymentMethodsService, PaymentProfileStore
from oolu.durable.connection import DurableConnection
from oolu.durable.files import UserFile, UserFileStore
from oolu.gateway import GatewayApp
from oolu.identity import AuthorityGrant, LocalAccountService, LocalUserStore, Role
from oolu.identity.google_signin import IdentityLinkStore
from oolu.mail import MailCodeStore
from oolu.social import AssistantHistoryStore, DirectMessageStore

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
PASSWORD = "alices-password-1"


def _host(tmp_path):
    app, conn, ident = _app(tmp_path)
    users = LocalUserStore(":memory:")
    accounts = LocalAccountService(users, ident.store, ident._signer)
    accounts.create_user("alice", PASSWORD, tenant="t1")
    accounts.create_user("bob", "bobs-password-1", tenant="t1")
    links = IdentityLinkStore(conn)
    links.link(
        provider="email", subject="alice@mphepo.io", tenant="t1",
        username="alice", email="alice@mphepo.io", at=NOW,
    )
    mail_codes = MailCodeStore(conn)
    mail_codes.mark_verified("alice@mphepo.io", "verify")
    gateway = GatewayApp(
        app._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        approval_authority=ident.authority,
        accounts=accounts,
        identity_links=links,
        direct_messages=DirectMessageStore(conn),
        assistant_history=AssistantHistoryStore(conn),
        mail_codes=mail_codes,
        files=UserFileStore(conn),
        payments=PaymentMethodsService(PaymentProfileStore(conn), FakeCardVault()),
        legal_dir=tmp_path / "legal",
    )
    return gateway, conn, ident


def _seed_alice(gateway):
    gateway._direct_messages.send(
        tenant="t1", sender="alice", recipient="bob", body="hey bob"
    )
    gateway._direct_messages.send(
        tenant="t1", sender="bob", recipient="alice", body="hey alice"
    )
    gateway._assistant_history.append(
        tenant="t1", principal="alice", kind="user", body="hi"
    )
    gateway._assistant_history.append(
        tenant="t1", principal="alice", kind="assistant", body="Hey! ⚡"
    )
    gateway._files.save(
        UserFile(
            file_id="f1", tenant_id="t1", node_id=None,
            name="notes.md", folder="", content="my notes",
            created_at=NOW, updated_at=NOW,
        )
    )
    gateway._payments.add_test_card("alice", "visa")


# --------------------------------------------------------------------------- #
# Export.                                                                      #
# --------------------------------------------------------------------------- #
def test_export_returns_everything_as_one_document(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    _seed_alice(gateway)

    export = gateway.handle(
        _req("GET", "/v1/account/export", token=ident.token("alice", "t1"))
    )
    assert export.status == 200, export.body
    body = export.body
    assert body["principal"] == "alice"
    assert body["account"]["username"] == "alice"
    assert [link["email"] for link in body["identity_links"]] == [
        "alice@mphepo.io"
    ]
    assert [t["body"] for t in body["chat"]] == ["hi", "Hey! ⚡"]
    assert [m["text"] for m in body["messages"]["bob"]] == [
        "hey bob",
        "hey alice",
    ]
    assert body["files"][0]["name"] == "notes.md"
    assert body["files"][0]["content"] == "my notes"
    assert body["payment_profile"]["cards"][0]["brand"] == "visa"
    assert "runs" in body
    conn.close()


# --------------------------------------------------------------------------- #
# Erasure.                                                                     #
# --------------------------------------------------------------------------- #
def test_delete_demands_the_password(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    refused = gateway.handle(
        _req(
            "POST",
            "/v1/account/delete",
            token=ident.token("alice", "t1"),
            body={"password": "not-it"},
        )
    )
    assert refused.status == 403
    assert "password" in refused.body["error"]["message"]
    # Nothing was touched.
    assert gateway._accounts.user("alice").disabled is False
    conn.close()


def test_delete_erases_disables_and_says_exactly_what_it_did(tmp_path):
    gateway, conn, ident = _host(tmp_path)
    _seed_alice(gateway)

    deleted = gateway.handle(
        _req(
            "POST",
            "/v1/account/delete",
            token=ident.token("alice", "t1"),
            body={"password": PASSWORD},
        )
    )
    assert deleted.status == 200, deleted.body
    assert deleted.body["account"] == "disabled"
    erased = deleted.body["erased"]
    assert erased["messages"] == 2  # both sides of the conversation
    assert erased["chat_turns"] == 2
    assert erased["identity_links"] == 1
    assert erased["mail_codes"] == 1
    assert erased["payment_profile"] == 1
    assert any("reserved" in note for note in deleted.body["notes"])

    # The stores are actually empty, the account actually closed.
    assert (
        gateway._direct_messages.conversations(tenant="t1", principal="alice")
        == []
    )
    assert (
        gateway._assistant_history.history(tenant="t1", principal="alice") == []
    )
    assert gateway._identity_links.links_for("alice") == []
    assert gateway._accounts.user("alice").disabled is True
    # Bob's own thread view is gone too — one shared copy, said plainly.
    assert (
        gateway._direct_messages.conversations(tenant="t1", principal="bob")
        == []
    )
    # The erasure itself is on the audit chain.
    events = gateway._durable.audit.records(run_id="account:alice")
    assert [e.event_type for e in events] == ["account.erased"]
    conn.close()


# --------------------------------------------------------------------------- #
# The legal surface.                                                           #
# --------------------------------------------------------------------------- #
def test_legal_urls_are_public_and_templates_are_unmissable(tmp_path):
    gateway, conn, _ = _host(tmp_path)
    terms = gateway.handle(_req("GET", "/v1/legal/terms"))
    privacy = gateway.handle(_req("GET", "/v1/legal/privacy"))
    assert terms.status == privacy.status == 200
    assert "TEMPLATE — NOT LEGAL ADVICE" in terms.body
    assert "TEMPLATE — NOT LEGAL ADVICE" in privacy.body
    assert terms.content_type.startswith("text/markdown")

    policy = gateway.handle(_req("GET", "/v1/legal/node-policy"))
    assert policy.status == 200
    assert policy.body["version"] and "no clones" in policy.body["text"]
    conn.close()


def test_the_operators_words_replace_the_template_verbatim(tmp_path):
    gateway, conn, _ = _host(tmp_path)
    legal = tmp_path / "legal"
    legal.mkdir()
    (legal / "terms.md").write_text("# Our real terms\nCounsel-approved.")

    terms = gateway.handle(_req("GET", "/v1/legal/terms"))
    assert terms.body == "# Our real terms\nCounsel-approved."
    assert "TEMPLATE" not in terms.body
    # privacy.md doesn't exist yet: its template still answers.
    privacy = gateway.handle(_req("GET", "/v1/legal/privacy"))
    assert "TEMPLATE — NOT LEGAL ADVICE" in privacy.body
    conn.close()


# --------------------------------------------------------------------------- #
# Backups.                                                                     #
# --------------------------------------------------------------------------- #
def test_backup_copies_live_databases_and_the_machine_key(tmp_path):
    import io
    import sqlite3

    from oolu import cli

    data = tmp_path / "data"
    data.mkdir()
    # A live database with real content, and the keyring's key file.
    conn = sqlite3.connect(data / "host.db")
    conn.execute("CREATE TABLE things (name TEXT)")
    conn.execute("INSERT INTO things VALUES ('kept')")
    conn.commit()  # deliberately left OPEN: backups run against live DBs
    (data / "machine.key").write_bytes(b"the-keyring-key")

    out = io.StringIO()
    code = cli.main(
        ["backup", "--data", str(data), "--out", str(tmp_path / "backups")],
        out=out,
    )
    conn.close()
    assert code == 0, out.getvalue()
    [folder] = list((tmp_path / "backups").iterdir())
    assert (folder / "machine.key").read_bytes() == b"the-keyring-key"
    copy = sqlite3.connect(folder / "host.db")
    assert copy.execute("SELECT name FROM things").fetchone()[0] == "kept"
    copy.close()


def test_backup_refuses_a_directory_with_nothing_to_save(tmp_path, capsys):
    import io

    from oolu import cli

    empty = tmp_path / "empty"
    empty.mkdir()
    code = cli.main(
        ["backup", "--data", str(empty), "--out", str(tmp_path / "b")],
        out=io.StringIO(),
    )
    assert code != 0
    assert "nothing to back up" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The operator's numbers.                                                      #
# --------------------------------------------------------------------------- #
def test_metrics_are_the_operators_not_every_members(tmp_path):
    app, conn, ident = _app(tmp_path)

    member = app.handle(
        _req("GET", "/v1/metrics", token=ident.token("someone", "t1"))
    )
    assert member.status == 403

    ident.store.add_role(
        Role(
            tenant_id="t1",
            name="monitoring",
            permissions=frozenset({"metrics:read"}),
        )
    )
    ident.store.add_grant(
        AuthorityGrant(
            tenant_id="t1",
            principal_id="prober",
            role_name="monitoring",
            granted_by="x",
        )
    )
    probed = app.handle(
        _req("GET", "/v1/metrics", token=ident.token("prober", "t1"))
    )
    assert probed.status == 200, probed.body
    assert probed.body["uptime_seconds"] >= 0
    assert "requests" in probed.body
    conn.close()
