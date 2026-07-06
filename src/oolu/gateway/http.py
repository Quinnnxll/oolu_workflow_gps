"""Transport-agnostic HTTP primitives for the gateway.

The gateway is written against plain :class:`Request`/:class:`Response` objects so
it is fully testable offline; a thin WSGI/ASGI adapter (the production seam) maps a
real server's requests onto these. A small template router resolves ``/{param}``
path segments, and helpers add the security headers and CORS handling every
response must carry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    now: datetime | None = None

    def __post_init__(self):
        # Normalize header names to lowercase for case-insensitive lookup.
        object.__setattr__(
            self, "headers", {k.lower(): v for k, v in self.headers.items()}
        )

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def bearer_token(self) -> str | None:
        value = self.header("authorization")
        if value and value.startswith("Bearer "):
            return value[len("Bearer ") :]
        return None


@dataclass
class Response:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "application/json"

    @property
    def json(self) -> Any:
        return self.body


def json_response(
    status: int, body: Any, headers: dict[str, str] | None = None
) -> Response:
    return Response(status=status, body=body, headers=dict(headers or {}))


def with_security_headers(response: Response) -> Response:
    for key, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


def apply_cors(
    response: Response, request: Request, allowed_origins: frozenset[str]
) -> Response:
    origin = request.header("origin")
    if origin and ("*" in allowed_origins or origin in allowed_origins):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = (
            "Authorization, Content-Type, Idempotency-Key"
        )
        response.headers["Vary"] = "Origin"
    return response


@dataclass(frozen=True)
class Route:
    method: str
    template: str
    handler: Callable
    requires_permission: str | None = None
    public: bool = False


class Router:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(
        self,
        method: str,
        template: str,
        handler: Callable,
        *,
        requires_permission: str | None = None,
        public: bool = False,
    ) -> None:
        self._routes.append(
            Route(method, template, handler, requires_permission, public)
        )

    def match(self, method: str, path: str) -> tuple[Route, dict[str, str]] | None:
        path_parts = path.rstrip("/").split("/")
        for route in self._routes:
            if route.method != method:
                continue
            template_parts = route.template.rstrip("/").split("/")
            if len(template_parts) != len(path_parts):
                continue
            params: dict[str, str] = {}
            matched = True
            for tmpl, actual in zip(template_parts, path_parts, strict=True):
                if tmpl.startswith("{") and tmpl.endswith("}"):
                    params[tmpl[1:-1]] = actual
                elif tmpl != actual:
                    matched = False
                    break
            if matched:
                return route, params
        return None

    def allowed_methods(self, path: str) -> set[str]:
        path_parts = path.rstrip("/").split("/")
        methods: set[str] = set()
        for route in self._routes:
            template_parts = route.template.rstrip("/").split("/")
            if len(template_parts) != len(path_parts):
                continue
            if all(
                tmpl.startswith("{") or tmpl == actual
                for tmpl, actual in zip(template_parts, path_parts, strict=True)
            ):
                methods.add(route.method)
        return methods
