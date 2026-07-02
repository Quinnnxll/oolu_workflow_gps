from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4

from ..orchestrator.planner import classify_risk
from .discovery import DiscoveredTool
from .ports import ActionExecutor
from .registry import RegisteredSkill, SkillRegistry

_JSON = {"Content-Type": "application/json", "X-Content-Type-Options": "nosniff"}


def _card(entry: RegisteredSkill, *, score: float | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {
        "skill_id": entry.skill_id,
        "semver": entry.semver,
        "name": entry.name,
        "summary": entry.summary,
        "tags": entry.tags,
        "content_hash": entry.content_hash,
        "parameters": [p.name for p in entry.skill.parameters],
        "operations": [a.operation for a in entry.skill.actions],
    }
    if score is not None:
        card["score"] = score
    return card


class SkillsServer:
    def __init__(
        self,
        registry: SkillRegistry,
        *,
        executors: dict[str, ActionExecutor] | None = None,
        tools: list[DiscoveredTool] | None = None,
    ):
        self._registry = registry
        self._executors = dict(executors or {})
        self._tools = list(tools or [])

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            raise RuntimeError(f"unsupported scope type: {scope['type']}")
        method, path = scope["method"], scope["path"]
        query = {
            k: v[0]
            for k, v in parse_qs(
                scope.get("query_string", b"").decode("latin1")
            ).items()
        }
        if method == "GET" and path == "/v1/tools":
            await self._json(
                send, 200, {"items": [t.model_dump() for t in self._tools]}
            )
        elif method == "GET" and path == "/v1/skills":
            await self._json(send, 200, self._list(query))
        elif method == "POST" and path == "/v1/skills/execute":
            body = await self._read_json(receive)
            status, payload = self._execute(body)
            await self._json(send, status, payload)
        elif method == "GET" and path.startswith("/v1/skills/"):
            await self._json(send, *self._get(path[len("/v1/skills/") :], query))
        else:
            await self._json(send, 404, {"error": "not_found"})

    def _list(self, query: dict[str, str]) -> dict[str, Any]:
        limit = max(1, min(100, int(query.get("limit", "20"))))
        term = query.get("q", "").strip()
        if term:
            scored = self._registry.search(term, limit=limit)
            return {"items": [_card(s.skill, score=s.score) for s in scored]}
        return {"items": [_card(s) for s in self._registry.list(limit=limit)]}

    def _get(self, skill_id: str, query: dict[str, str]) -> tuple[int, dict[str, Any]]:
        entry = self._registry.get(skill_id, semver=query.get("semver"))
        if entry is None:
            return 404, {"error": "not_found"}
        card = _card(entry)
        card["skill"] = entry.skill.model_dump(mode="json")
        card["versions"] = self._registry.versions(skill_id)
        return 200, card

    def _execute(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        skill_id = body.get("skill_id")
        if not isinstance(skill_id, str):
            return 400, {"error": "skill_id is required"}
        entry = self._registry.get(skill_id, semver=body.get("semver"))
        if entry is None:
            return 404, {"error": "not_found"}
        overrides = body.get("parameters") or {}
        run_key = body.get("idempotency_key") or uuid4().hex
        outcomes: list[dict[str, Any]] = []
        for index, action in enumerate(entry.skill.actions):
            if classify_risk(action.operation) == "irreversible":
                return 409, {
                    "error": "irreversible_action",
                    "detail": f"{action.operation} requires approval via the run flow",
                }
            executor = self._executors.get(action.adapter)
            if executor is None or action.operation not in executor.capabilities():
                return 422, {
                    "error": "missing_capability",
                    "detail": f"{action.adapter}/{action.operation}",
                }
            merged = action.model_copy(
                update={"parameters": {**action.parameters, **overrides}}
            )
            outcome = executor.execute(
                merged, idempotency_key=f"{skill_id}:{entry.semver}:{index}:{run_key}"
            )
            outcomes.append(outcome.model_dump(mode="json"))
            if outcome.status.value != "succeeded":
                break
        return 200, {
            "skill_id": skill_id,
            "semver": entry.semver,
            "outcomes": outcomes,
        }

    @staticmethod
    async def _read_json(receive: Any) -> dict[str, Any]:
        chunks: list[bytes] = []
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

    @staticmethod
    async def _json(send: Any, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(k.encode(), v.encode()) for k, v in _JSON.items()],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _lifespan(receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
