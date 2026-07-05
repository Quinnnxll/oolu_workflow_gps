from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import parse_qs

from ..identity.errors import AuthenticationError, AuthorizationError
from .service import DesktopService
from .ui import INDEX_HTML

# The desktop UI binds here over 127.0.0.1 only. This is the loopback boundary
# ADR-0004 names: secret-free view-models, no execution path, and NO auth — the
# multi-tenant OIDC gateway remains the door for web/mobile. Never bind this to
# a non-loopback interface. The single exception to "no auth": the approval
# decision route REQUIRES a bearer token, because approvals must mint from a
# verified identity even locally — the service validates it, never this layer.
_TASK_RE = re.compile(r"^/v1/tasks/(?P<run_id>[^/]+)(?P<rest>/[a-z-]+)?$")
_APPROVAL_RE = re.compile(r"^/v1/assembly/approvals/(?P<pending_id>[^/]+)$")
_JSON = {
    "Content-Type": "application/json",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


class DesktopLoopbackApp:
    def __init__(self, service: DesktopService, *, registry: Any = None):
        self._svc = service
        self._registry = registry

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await _lifespan(receive, send)
            return
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
            return
        if scope["type"] != "http":
            raise RuntimeError(f"unsupported scope type: {scope['type']}")

        method, path = scope["method"], scope["path"]
        if method == "GET" and path in ("/", "/index.html"):
            # The scaffold UI: one self-contained page over this same API.
            await _send_html(send, INDEX_HTML)
            return
        query = {
            k: v[0]
            for k, v in parse_qs(
                scope.get("query_string", b"").decode("latin1")
            ).items()
        }
        try:
            status, payload = await self._route(method, path, query, receive, scope)
        except KeyError:
            status, payload = 404, {"error": "not_found"}
        except AuthenticationError as exc:
            status, payload = 401, {"error": str(exc)}
        except (AuthorizationError, PermissionError) as exc:
            status, payload = 403, {"error": str(exc)}
        except _BadRequest as exc:
            status, payload = 400, {"error": str(exc)}
        await _send_json(send, status, payload)

    async def _route(self, method, path, query, receive, scope=None):
        if method == "GET" and path == "/v1/inbox":
            items = self._svc.inbox(query.get("kind"))
            return 200, {"items": [i.model_dump(mode="json") for i in items]}
        if method == "GET" and path == "/v1/skills":
            return 200, self._skills(query)
        if method == "GET" and path == "/v1/worker-health":
            return 200, self._svc.worker_health().model_dump(mode="json")
        if method == "GET" and path == "/v1/earnings":
            return 200, self._svc.earnings().model_dump(mode="json")
        if method == "GET" and path == "/v1/payout-account":
            return 200, self._svc.payout_account().model_dump(mode="json")
        if method == "POST" and path == "/v1/payout-account":
            body = await _read_json(receive)
            view = self._svc.onboard_payout_account(
                country=str(body.get("country", "US")),
                currency=str(body.get("currency", "usd")),
            )
            return 201, view.model_dump(mode="json")
        if method == "GET" and path == "/v1/offline-policy":
            return 200, self._svc.offline_policy()
        if method == "POST" and path == "/v1/tasks":
            body = await _read_json(receive)
            intent = body.get("intent")
            if not intent:
                raise _BadRequest("intent is required")
            return 201, self._svc.submit_task(intent).model_dump(mode="json")
        if method == "POST" and path == "/v1/assembly/preview":
            body = await _read_json(receive)
            if not body.get("goal") or not body.get("want"):
                raise _BadRequest("goal and want are required")
            try:
                view = self._svc.assembly_preview(
                    goal=str(body["goal"]),
                    want=list(body["want"]),
                    have=list(body.get("have", [])),
                    query=str(body.get("q", "")),
                    fill_gaps=bool(body.get("fill_gaps", False)),
                    explore=bool(body.get("explore", False)),
                    budget_cap=_maybe_float(body.get("budget_cap")),
                    review_threshold=_maybe_float(body.get("review_threshold")),
                )
            except (ValueError, TypeError) as exc:
                raise _BadRequest(str(exc)) from exc
            return 200, view.model_dump(mode="json")
        approval = _APPROVAL_RE.match(path)
        if method == "POST" and approval:
            body = await _read_json(receive)
            if "approved" not in body:
                raise _BadRequest("approved (true or false) is required")
            token = _bearer(scope)
            if not token:
                raise AuthenticationError(
                    "an Authorization bearer token is required to decide "
                    "a held contract"
                )
            view = self._svc.decide_assembly(
                approval["pending_id"],
                token=token,
                approved=bool(body["approved"]),
                required_assurance=int(body.get("required_assurance", 1)),
            )
            return 200, view.model_dump(mode="json")
        if method == "POST" and path == "/v1/assembly/confirm":
            body = await _read_json(receive)
            if not isinstance(body.get("contract"), dict):
                raise _BadRequest("a contract object is required")
            confirm_id = str(body.get("confirm_id") or "") or None
            try:
                # PermissionError propagates -> 403 (budget caps and
                # unacknowledged review reasons; reserved contracts are
                # HELD as approvable inbox tasks, not refused).
                view = self._svc.confirm_assembly(
                    body["contract"],
                    confirm_id=confirm_id,
                    budget_cap=_maybe_float(body.get("budget_cap")),
                    review_threshold=_maybe_float(body.get("review_threshold")),
                    review_acknowledged=bool(body.get("review_acknowledged", False)),
                )
            except (ValueError, TypeError) as exc:
                raise _BadRequest(str(exc)) from exc
            return 200, view.model_dump(mode="json")
        match = _TASK_RE.match(path)
        if match:
            return await self._task_route(
                method, match["run_id"], match["rest"], receive
            )
        return 404, {"error": "not_found"}

    async def _task_route(self, method, run_id, rest, receive):
        if method == "GET" and rest is None:
            return 200, self._svc.task(run_id).model_dump(mode="json")
        if method == "GET" and rest == "/timeline":
            return 200, {"items": self._timeline(run_id)}
        if method == "GET" and rest == "/route":
            return 200, self._svc.route_preview(run_id).model_dump(mode="json")
        if method == "GET" and rest == "/audit":
            return 200, self._svc.audit(run_id).model_dump(mode="json")
        if method == "POST" and rest == "/answers":
            answers = (await _read_json(receive)).get("answers", {})
            return 200, self._svc.answer_questions(run_id, answers).model_dump(
                mode="json"
            )
        if method == "POST" and rest == "/confirm":
            approved = bool((await _read_json(receive)).get("approved", False))
            return 200, self._svc.confirm(run_id, approved=approved).model_dump(
                mode="json"
            )
        if method == "POST" and rest == "/resolve-incident":
            decision = (await _read_json(receive)).get("decision", "abort")
            return 200, self._svc.resolve_incident(
                run_id, decision=decision
            ).model_dump(mode="json")
        if method == "POST" and rest == "/cancel":
            return 200, self._svc.cancel(run_id).model_dump(mode="json")
        return 404, {"error": "not_found"}

    def _skills(self, query):
        if self._registry is None:
            return {"items": []}
        term = query.get("q", "").strip()
        limit = max(1, min(100, int(query.get("limit", "50"))))
        if term:
            scored = self._registry.search(term, limit=limit)
            return {"items": [_skill_card(s.skill, score=s.score) for s in scored]}
        return {"items": [_skill_card(s) for s in self._registry.list(limit=limit)]}

    def _timeline(self, run_id):
        return [event.model_dump(mode="json") for event in self._svc.timeline(run_id)]

    async def _websocket(self, scope, receive, send):
        connect = await receive()
        if connect.get("type") != "websocket.connect":
            return
        match = _TASK_RE.match(scope["path"])
        if match is None or match["rest"] != "/events":
            await send({"type": "websocket.close", "code": 4404})
            return
        run_id = match["run_id"]
        try:
            self._svc.task(run_id)  # existence check
        except KeyError:
            await send({"type": "websocket.close", "code": 4404})
            return
        await send({"type": "websocket.accept"})
        # Poll-then-wait: push new timeline events (append-only) until disconnect.
        sent = 0
        while True:
            events = self._timeline(run_id)
            for event in events[sent:]:
                await send({"type": "websocket.send", "text": json.dumps(event)})
            sent = len(events)
            try:
                message = await asyncio.wait_for(receive(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if message.get("type") == "websocket.disconnect":
                return


class _BadRequest(Exception):
    pass


def _bearer(scope) -> str | None:
    """The Authorization bearer token from the request headers, if any."""
    for name, value in (scope or {}).get("headers") or []:
        if name.lower() == b"authorization":
            text = value.decode("latin1").strip()
            if text.lower().startswith("bearer "):
                return text[7:].strip() or None
            return None
    return None


def _maybe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise _BadRequest(f"not a number: {value!r}") from exc


def _skill_card(entry, *, score=None):
    card: dict[str, Any] = {
        "skill_id": entry.skill_id,
        "semver": entry.semver,
        "name": entry.name,
        "summary": entry.summary,
        "tags": entry.tags,
    }
    if score is not None:
        card["score"] = score
    return card


async def _read_json(receive):
    chunks = []
    more = True
    while more:
        message = await receive()
        chunks.append(message.get("body", b"") or b"")
        more = message.get("more_body", False)
    raw = b"".join(chunks)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _send_html(send, body: str):
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"x-content-type-options", b"nosniff"),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body.encode("utf-8")})


async def _send_json(send, status, payload):
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(k.encode(), v.encode()) for k, v in _JSON.items()],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _lifespan(receive, send):
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return
