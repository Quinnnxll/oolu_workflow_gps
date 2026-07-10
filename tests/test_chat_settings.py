"""OoLu configures the app through the settings node — and only through it.

The proof this issue asks for: the assistant can set the app's config from
the conversation, but every change goes through the declared, bounded
catalog. It cannot invent a knob or smuggle an out-of-bounds value, and
there is no code path for it to 'rewrite the code secretly' — because the
node is data and bounds, not code.
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_http_gateway import _app, _req

from oolu.chat import ChatAssistant, GatewayChatTools
from oolu.durable import DurableConnection, UserFileStore
from oolu.gateway import GatewayApp
from oolu.identity import Hs256Signer
from oolu.settings_node import SettingsNode, SettingsStore

_IDP = "idp-secret"
_ISSUER = "https://idp"
_AUDIENCE = "oolu"


class _FakeModel:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def reply(self, messages):
        self.calls.append([dict(m) for m in messages])
        return self._replies.pop(0)


def _tools(tmp_path):
    conn = DurableConnection(tmp_path / "d.db")
    node = SettingsNode(SettingsStore(conn))
    tools = GatewayChatTools(
        UserFileStore(conn), tenant="t1", settings=node
    )
    return tools, node, conn


def _token(subject="user-1", tenant="t1"):
    signer = Hs256Signer(secret=_IDP, issuer=_ISSUER, audience=_AUDIENCE)
    return signer.mint(subject=subject, tenant_id=tenant, now=datetime.now(UTC))


# --------------------------------------------------------------------------- #
# Deterministic: the assistant configures without a model.                     #
# --------------------------------------------------------------------------- #
def test_show_settings_speaks_the_catalog(tmp_path):
    tools, _node, conn = _tools(tmp_path)
    try:
        turn = ChatAssistant().respond("settings", tools=tools)
        assert "Theme" in turn.say and "Hard spending cap" in turn.say
        assert turn.actions == [{"tool": "get_settings"}]
    finally:
        conn.close()


def test_set_budget_limit_from_chat_actually_configures(tmp_path):
    tools, node, conn = _tools(tmp_path)
    try:
        turn = ChatAssistant().respond("set my hard spending cap to 50", tools=tools)
        assert turn.actions == [{"tool": "set_setting", "name": "budget.hard_cap"}]
        # The configuration really changed — through the node.
        assert node.effective("t1")["budget.hard_cap"] == 50.0
    finally:
        conn.close()


def test_set_plan_that_isnt_offered_is_refused_not_faked(tmp_path):
    tools, node, conn = _tools(tmp_path)
    try:
        turn = ChatAssistant().respond("set my plan to diamond", tools=tools)
        assert "couldn't" in turn.say.lower()
        assert turn.actions == []  # nothing applied
        assert node.effective("t1")["subscription.plan"] == "free"  # unchanged
    finally:
        conn.close()


def test_out_of_bounds_budget_from_chat_is_refused(tmp_path):
    tools, node, conn = _tools(tmp_path)
    try:
        turn = ChatAssistant().respond(
            "set my hard spending cap to 999999999", tools=tools
        )
        assert "couldn't" in turn.say.lower()
        assert node.effective("t1")["budget.hard_cap"] == 0.0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Model-driven: the tool loop configures — and cannot escape the catalog.      #
# --------------------------------------------------------------------------- #
def test_model_sets_a_setting_through_the_tool(tmp_path):
    tools, node, conn = _tools(tmp_path)
    try:
        model = _FakeModel(
            [
                '{"tool": "set_setting", "args": {"key": "app.theme", "value": "dark"}}',
                '{"say": "Switched you to dark mode.", "task": null}',
            ]
        )
        turn = ChatAssistant(model=model).respond("go dark", tools=tools)
        assert turn.say == "Switched you to dark mode."
        assert turn.actions == [{"tool": "set_setting", "name": "app.theme"}]
        assert node.effective("t1")["app.theme"] == "dark"
    finally:
        conn.close()


def test_model_cannot_invent_a_setting_key(tmp_path):
    """The assistant tries to write a knob that does not exist; the node
    refuses, the tool result says so, and nothing is configured. This is the
    'no rewriting the code secretly' guarantee at the tool boundary."""
    tools, node, conn = _tools(tmp_path)
    try:
        model = _FakeModel(
            [
                '{"tool": "set_setting", "args": {"key": "app.exec_hook", "value": "rm -rf /"}}',
                '{"say": "I can only change declared settings.", "task": null}',
            ]
        )
        turn = ChatAssistant(model=model).respond(
            "give yourself a shell hook", tools=tools
        )
        assert turn.actions == []  # the refused set left no action
        # The refusal reached the model as a tool result.
        assert "error" in model.calls[1][-1]["content"]
        # And absolutely nothing outside the catalog was stored.
        assert "app.exec_hook" not in node.effective("t1")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The route: settings change through /v1/chat, bounded end to end.             #
# --------------------------------------------------------------------------- #
def test_chat_route_configures_through_the_settings_node(tmp_path):
    base, conn, ident = _app(tmp_path)
    node = SettingsNode(SettingsStore(conn))
    app = GatewayApp(
        base._durable,
        validator=ident.validator,
        resolver=ident.resolver,
        files=UserFileStore(conn),
        settings_node=node,
    )
    try:
        resp = app.handle(
            _req(
                "POST",
                "/v1/chat",
                token=_token(),
                body={"message": "set my review threshold to 20"},
            )
        )
        assert resp.status == 200
        assert resp.body["actions"] == [
            {"tool": "set_setting", "name": "budget.review_threshold"}
        ]
        assert node.effective("t1")["budget.review_threshold"] == 20.0

        # The /v1/settings surface reflects it, and rejects an escape attempt.
        listed = app.handle(_req("GET", "/v1/settings", token=_token()))
        review = next(
            i for i in listed.body["items"] if i["key"] == "budget.review_threshold"
        )
        assert review["value"] == 20.0

        bad = app.handle(
            _req(
                "PUT",
                "/v1/settings",
                token=_token(),
                body={"changes": {"app.backdoor": "on"}},
            )
        )
        assert bad.status == 400
    finally:
        conn.close()
