"""The assistant's hands: chat file tools.

Reading and writing the user's files from the conversation — deterministic
exact commands on model-less installs, a bounded tool-call loop when a
model drives, and an audited ``actions`` trail either way so the UI can
show what was touched.
"""

from __future__ import annotations

from test_http_gateway import _app, _req

from oolu.chat import ChatAssistant, FileChatTools
from oolu.durable import DurableConnection, UserFile, UserFileStore
from oolu.gateway import GatewayApp
from oolu.identity import Hs256Signer

_IDP = "idp-secret"
_ISSUER = "https://idp"
_AUDIENCE = "oolu"


class _FakeModel:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    def reply(self, messages):
        self.calls.append([dict(m) for m in messages])
        return self._replies.pop(0)


def _tools(tmp_path, *files: UserFile):
    conn = DurableConnection(tmp_path / "d.db")
    store = UserFileStore(conn)
    for file in files:
        store.save(file)
    return FileChatTools(store, tenant="t1"), store, conn


def _f(name, content=""):
    return UserFile(tenant_id="t1", name=name, content=content)


# --------------------------------------------------------------------------- #
# Model-less: exact commands, everything else stays the intent.                #
# --------------------------------------------------------------------------- #
def test_list_files_speaks_the_inventory(tmp_path):
    tools, _, conn = _tools(tmp_path, _f("notes.md", "hi"), _f("budget.csv", "a,b"))
    try:
        turn = ChatAssistant().respond("list files", tools=tools)
        assert turn.task is None
        assert "notes.md" in turn.say and "budget.csv" in turn.say
        assert turn.actions == [{"tool": "list_files"}]
    finally:
        conn.close()


def test_read_by_name_and_by_unique_substring(tmp_path):
    tools, _, conn = _tools(tmp_path, _f("launch-notes.md", "ship it"))
    try:
        exact = ChatAssistant().respond("read launch-notes.md", tools=tools)
        assert "ship it" in exact.say
        fuzzy = ChatAssistant().respond("show launch", tools=tools)
        assert "ship it" in fuzzy.say
        assert fuzzy.actions == [{"tool": "read_file", "name": "launch-notes.md"}]
    finally:
        conn.close()


def test_ambiguous_read_asks_instead_of_guessing(tmp_path):
    tools, _, conn = _tools(tmp_path, _f("q3-plan.md"), _f("q3-budget.csv"))
    try:
        turn = ChatAssistant().respond("open q3", tools=tools)
        assert turn.task is None and turn.actions == []
        assert "q3-plan.md" in turn.say and "q3-budget.csv" in turn.say
    finally:
        conn.close()


def test_read_of_a_missing_file_falls_through_to_work(tmp_path):
    tools, _, conn = _tools(tmp_path, _f("notes.md"))
    try:
        turn = ChatAssistant().respond("show me a good pasta recipe", tools=tools)
        assert turn.source == "intent"
        assert turn.task == "show me a good pasta recipe"
    finally:
        conn.close()


def test_write_creates_and_append_extends(tmp_path):
    tools, store, conn = _tools(tmp_path)
    try:
        created = ChatAssistant().respond(
            "write to shopping.md: milk", tools=tools
        )
        assert created.actions == [{"tool": "write_file", "name": "shopping.md"}]
        appended = ChatAssistant().respond(
            "append to shopping.md: eggs", tools=tools
        )
        assert "Saved shopping.md" in appended.say
        (file,) = store.list(tenant="t1")
        assert file.content == "milk\neggs"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Model-driven: the bounded tool loop.                                         #
# --------------------------------------------------------------------------- #
def test_model_reads_a_file_then_answers(tmp_path):
    tools, _, conn = _tools(tmp_path, _f("launch-notes.md", "ship on friday"))
    try:
        model = _FakeModel(
            [
                '{"tool": "read_file", "args": {"name": "launch-notes.md"}}',
                '{"say": "You planned to ship on Friday.", "task": null}',
            ]
        )
        turn = ChatAssistant(model=model).respond(
            "when did I plan to ship?", tools=tools
        )
        assert turn.say == "You planned to ship on Friday."
        assert turn.actions == [{"tool": "read_file", "name": "launch-notes.md"}]
        # The tool result reached the model on the second round.
        assert "ship on friday" in model.calls[1][-1]["content"]
    finally:
        conn.close()


def test_model_writes_a_file(tmp_path):
    tools, store, conn = _tools(tmp_path)
    try:
        model = _FakeModel(
            [
                '{"tool": "write_file", "args": {"name": "todo.md", "content": "buy milk"}}',
                '{"say": "Saved your list.", "task": null}',
            ]
        )
        turn = ChatAssistant(model=model).respond("note down: buy milk", tools=tools)
        assert turn.actions == [{"tool": "write_file", "name": "todo.md"}]
        (file,) = store.list(tenant="t1")
        assert file.content == "buy milk"
    finally:
        conn.close()


def test_tool_loop_is_bounded(tmp_path):
    tools, _, conn = _tools(tmp_path, _f("a.md", "x"))
    try:
        model = _FakeModel(
            ['{"tool": "list_files", "args": {}}'] * 10  # never speaks
        )
        turn = ChatAssistant(model=model).respond("loop forever", tools=tools)
        assert "tangled" in turn.say
        assert len(model.calls) == 4  # MAX_TOOL_ROUNDS
    finally:
        conn.close()


def test_model_tool_call_without_tools_degrades_honestly(tmp_path):
    model = _FakeModel(['{"tool": "read_file", "args": {"name": "x"}}'])
    turn = ChatAssistant(model=model).respond("read x")
    assert "can't reach any files" in turn.say
    assert turn.task is None


# --------------------------------------------------------------------------- #
# The route: tools bound to the caller's tenant, actions in the reply.         #
# --------------------------------------------------------------------------- #
def _token(subject="user-1", tenant="t1"):
    signer = Hs256Signer(secret=_IDP, issuer=_ISSUER, audience=_AUDIENCE)
    from datetime import UTC, datetime

    return signer.mint(subject=subject, tenant_id=tenant, now=datetime.now(UTC))


def test_chat_route_reads_and_writes_tenant_files(tmp_path):
    base, conn, ident = _app(tmp_path)
    store = UserFileStore(conn)
    store.save(UserFile(tenant_id="t1", name="notes.md", content="hello from t1"))
    store.save(UserFile(tenant_id="t2", name="secret.md", content="t2 only"))
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        files=store,
    )
    try:
        read = app.handle(
            _req("POST", "/v1/chat", token=_token(), body={"message": "read notes.md"})
        )
        assert read.status == 200
        assert "hello from t1" in read.body["reply"]
        assert read.body["actions"] == [{"tool": "read_file", "name": "notes.md"}]
        assert read.body["run_id"] is None

        # Another tenant's file is invisible: the message becomes work.
        blind = app.handle(
            _req("POST", "/v1/chat", token=_token(), body={"message": "read secret.md"})
        )
        assert blind.body["run_id"] is not None

        write = app.handle(
            _req(
                "POST",
                "/v1/chat",
                token=_token(),
                body={"message": "append to notes.md: more"},
            )
        )
        assert write.status == 200
        assert store.list(tenant="t1")[0].content == "hello from t1\nmore"
    finally:
        conn.close()
