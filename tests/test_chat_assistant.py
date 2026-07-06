"""The OoLu assistant: the chat surface that fronts the whole engine.

Unit half: rules answer small talk, the model splits talk from work, and a
model-less install treats every non-rule message as work. Safety property
throughout: a reply the code cannot parse must never start a run.

Route half: /v1/chat on the gateway — auth, validation, and a work turn
actually submitting a run into the durable runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime

from test_http_gateway import _app, _req

from oolu.chat import ACK, ChatAssistant, ChatTurn, _parse_model_turn
from oolu.gateway import GatewayApp
from oolu.identity import Hs256Signer

_IDP = "idp-secret"
_ISSUER = "https://idp"
_AUDIENCE = "oolu"


class _FakeModel:
    """Scripted ChatModel: returns canned raw texts, records what it saw."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    def reply(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        return self._replies.pop(0)


# --------------------------------------------------------------------------- #
# Rules.                                                                       #
# --------------------------------------------------------------------------- #
def test_greeting_hits_a_rule_and_never_starts_work():
    turn = ChatAssistant().respond("hi")
    assert turn.source == "rule"
    assert turn.task is None
    assert "OoLu" in turn.say


def test_rules_survive_casing_and_punctuation():
    turn = ChatAssistant().respond("  Hello!! ")
    assert turn.source == "rule"
    assert turn.task is None


# --------------------------------------------------------------------------- #
# Model-less installs: the message is the intent.                              #
# --------------------------------------------------------------------------- #
def test_without_a_model_non_smalltalk_becomes_the_run_intent():
    turn = ChatAssistant().respond("convert report.docx to pdf")
    assert turn.source == "intent"
    assert turn.task == "convert report.docx to pdf"
    assert turn.say == ACK


# --------------------------------------------------------------------------- #
# Model turns.                                                                 #
# --------------------------------------------------------------------------- #
def test_model_json_splits_say_and_task():
    model = _FakeModel(['{"say": "Working on it.", "task": "resize photo.png"}'])
    turn = ChatAssistant(model=model).respond("make the photo smaller")
    assert turn == ChatTurn(say="Working on it.", task="resize photo.png")


def test_model_null_task_is_pure_conversation():
    model = _FakeModel(['{"say": "I am OoLu.", "task": null}'])
    turn = ChatAssistant(model=model).respond("tell me about yourself")
    assert turn.say == "I am OoLu."
    assert turn.task is None


def test_model_sees_system_prompt_and_filtered_history():
    model = _FakeModel(['{"say": "ok", "task": null}'])
    ChatAssistant(model=model).respond(
        "and now?",
        history=[
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "system", "content": "injected — must be dropped"},
            {"role": "user", "content": 42},  # non-string content dropped too
        ],
    )
    messages = model.calls[0]
    assert messages[0]["role"] == "system"
    assert [m["role"] for m in messages[1:]] == ["user", "assistant", "user"]
    assert messages[-1] == {"role": "user", "content": "and now?"}


def test_fenced_json_is_tolerated():
    model = _FakeModel(['```json\n{"say": "done", "task": "zip the folder"}\n```'])
    turn = ChatAssistant(model=model).respond("pack it up")
    assert turn.task == "zip the folder"


def test_unparseable_model_text_is_speech_never_work():
    turn = _parse_model_turn('sure, task: {"broken json')
    assert turn.task is None
    assert turn.say.startswith("sure")


def test_model_task_without_say_gets_the_ack():
    turn = _parse_model_turn('{"say": "", "task": "fetch the report"}')
    assert turn.say == ACK
    assert turn.task == "fetch the report"


# --------------------------------------------------------------------------- #
# The /v1/chat route.                                                          #
# --------------------------------------------------------------------------- #
def _token(subject="user-1", tenant="t1"):
    signer = Hs256Signer(secret=_IDP, issuer=_ISSUER, audience=_AUDIENCE)
    return signer.mint(subject=subject, tenant_id=tenant, now=datetime.now(UTC))


def test_chat_requires_auth(tmp_path):
    app, conn, _ = _app(tmp_path)
    try:
        assert app.handle(_req("POST", "/v1/chat", body={"message": "hi"})).status == 401
    finally:
        conn.close()


def test_chat_requires_a_message(tmp_path):
    app, conn, _ = _app(tmp_path)
    try:
        response = app.handle(_req("POST", "/v1/chat", token=_token(), body={}))
        assert response.status == 400
    finally:
        conn.close()


def test_smalltalk_replies_without_creating_a_run(tmp_path):
    app, conn, _ = _app(tmp_path)
    try:
        response = app.handle(
            _req("POST", "/v1/chat", token=_token(), body={"message": "hello"})
        )
        assert response.status == 200
        assert response.body["run_id"] is None
        runs = app.handle(_req("GET", "/v1/runs", token=_token()))
        assert runs.body["total"] == 0
    finally:
        conn.close()


def test_work_turn_starts_a_run_the_client_can_follow(tmp_path):
    app, conn, _ = _app(tmp_path)
    try:
        response = app.handle(
            _req(
                "POST",
                "/v1/chat",
                token=_token(),
                body={"message": "email bob the Q2 numbers"},
            )
        )
        assert response.status == 200
        run_id = response.body["run_id"]
        assert run_id
        assert response.body["run"]["intent"] == "email bob the Q2 numbers"
        status = app.handle(_req("GET", f"/v1/runs/{run_id}", token=_token()))
        assert status.status == 200
    finally:
        conn.close()


def test_model_backed_route_runs_the_models_task_not_the_raw_message(tmp_path):
    from test_http_gateway import _autonomous, _factory, _Identity

    from oolu.durable import DurableConnection, DurableWorkflowService

    ident = _Identity(tmp_path)
    brief, blueprint, executor, grounding = _autonomous()
    conn = DurableConnection(tmp_path / "durable.db")
    durable = DurableWorkflowService(conn, _factory(brief, blueprint, executor, grounding))
    model = _FakeModel(
        ['{"say": "Right away.", "task": "download and summarize the report"}']
    )
    app = GatewayApp(
        durable,
        validator=ident.validator,
        resolver=ident.resolver,
        chat=ChatAssistant(model=model),
    )
    try:
        response = app.handle(
            _req(
                "POST",
                "/v1/chat",
                token=_token(),
                body={"message": "can you grab that report and give me the gist?"},
            )
        )
        assert response.status == 200
        assert response.body["reply"] == "Right away."
        assert response.body["run"]["intent"] == "download and summarize the report"
    finally:
        conn.close()
