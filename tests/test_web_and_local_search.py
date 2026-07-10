"""Issue 13: the model's web search, and the desktop's own disk.

Two doors, honestly walled. (1) Web: the model may use its provider's
server-side web-search tool — the search runs inside the API call on
Anthropic's servers, so a keyed OoLu (own key or the Global subscription
brain) answers current-facts questions from ANY install; a local model
never searches (local means local), and the model.web_search setting
turns the door off. (2) Disk: the DESKTOP's chat can find files on the
user's own computer — home-rooted, listing only, bounded — while a
multi-user host never gets the tool at all: a server has no business in
anyone's home directory.
"""

from __future__ import annotations

from pathlib import Path

from test_chat_model_router import FakeTransport, _anthropic_reply

from oolu.billing import ModelCallMeter
from oolu.chat import ChatAssistant, GatewayChatTools
from oolu.durable.connection import DurableConnection
from oolu.durable.files import UserFileStore
from oolu.providers.chatmodel import ChatModelRouter
from oolu.providers.keyring import ModelKeyring


# --------------------------------------------------------------------------- #
# The web door.                                                                #
# --------------------------------------------------------------------------- #
def _router(tmp_path, *, web_search: bool):
    conn = DurableConnection(tmp_path / "durable.db")
    keyring = ModelKeyring(conn, key_path=tmp_path / "machine.key")
    keyring.store("t1", "anthropic", "sk-ant-test-key")
    transport = FakeTransport()
    router = ChatModelRouter(
        keyring,
        "t1",
        transport=transport,
        meter=ModelCallMeter(),
        source=lambda: "own-api",
        preference=lambda: "anthropic",
        web_search=lambda: web_search,
    )
    return conn, router, transport

def test_the_model_searches_the_web_inside_its_own_api_call(tmp_path):
    conn, router, transport = _router(tmp_path, web_search=True)
    transport.script("anthropic.com", 200, _anthropic_reply("It's sunny."))

    assert router.reply([{"role": "user", "content": "weather now?"}])
    [request] = transport.requests
    [tool] = request["body"]["tools"]
    assert tool["type"] == "web_search_20250305"
    assert tool["name"] == "web_search"
    assert tool["max_uses"] == 3
    conn.close()


def test_the_setting_closes_the_web_door(tmp_path):
    conn, router, transport = _router(tmp_path, web_search=False)
    transport.script("anthropic.com", 200, _anthropic_reply("Offline words."))

    assert router.reply([{"role": "user", "content": "hi"}])
    [request] = transport.requests
    assert "tools" not in request["body"]
    conn.close()


def test_the_catalog_carries_the_web_search_knob():
    from oolu.settings_node import SETTINGS_CATALOG

    [field] = [f for f in SETTINGS_CATALOG if f.key == "model.web_search"]
    assert field.default is True and field.group == "model"


# --------------------------------------------------------------------------- #
# The desktop's own disk.                                                      #
# --------------------------------------------------------------------------- #
def _disk(tmp_path) -> Path:
    root = tmp_path / "home"
    (root / "documents").mkdir(parents=True)
    (root / "documents" / "tax-report-2026.pdf").write_bytes(b"x" * 10)
    (root / "documents" / "notes.md").write_text("hello")
    (root / ".git").mkdir()
    (root / ".git" / "tax-secret.pdf").write_bytes(b"y")  # hidden: skipped
    return root


def _tools(tmp_path, root):
    conn = DurableConnection(tmp_path / "files.db")
    tools = GatewayChatTools(
        UserFileStore(conn), tenant="t1", local_root=root
    )
    return conn, tools


def test_the_desktop_finds_its_own_files_listing_only(tmp_path):
    conn, tools = _tools(tmp_path, _disk(tmp_path))
    assert tools.local_search_enabled()

    by_name = tools.search_local_files("tax")
    assert [m["path"] for m in by_name] == [
        str(Path("documents") / "tax-report-2026.pdf")
    ]
    assert by_name[0]["size"] == 10
    # A glob works too, and hidden directories never leak.
    by_glob = tools.search_local_files("*.pdf")
    assert len(by_glob) == 1
    assert tools.search_local_files("") == []
    conn.close()


def test_a_server_has_no_business_in_anyones_home(tmp_path):
    conn, tools = _tools(tmp_path, None)  # a host: no local root
    assert not tools.local_search_enabled()
    assert tools.search_local_files("tax") == []
    conn.close()


def test_the_chat_tool_answers_on_desktop_and_refuses_on_hosts(tmp_path):
    class _Model:
        def __init__(self, replies):
            self._replies = list(replies)

        def reply(self, messages):
            return self._replies.pop(0)

    # Desktop: the tool call lands, the listing feeds the answer.
    conn, tools = _tools(tmp_path, _disk(tmp_path))
    assistant = ChatAssistant()
    turn = assistant.respond(
        "find my tax report on this computer",
        tools=tools,
        model=_Model(
            [
                '{"tool": "find_local_files", "args": {"pattern": "tax"}}',
                '{"say": "Found it: documents/tax-report-2026.pdf", "task": null}',
            ]
        ),
    )
    assert "tax-report-2026.pdf" in turn.say
    assert {"tool": "find_local_files"} in turn.actions
    conn.close()

    # A host without a local root refuses in words the model can relay.
    conn2, host_tools = _tools(tmp_path, None)
    turn2 = ChatAssistant().respond(
        "find my tax report",
        tools=host_tools,
        model=_Model(
            [
                '{"tool": "find_local_files", "args": {"pattern": "tax"}}',
                '{"say": "That search lives on the desktop app.", "task": null}',
            ]
        ),
    )
    assert "desktop app" in turn2.say
    conn2.close()
