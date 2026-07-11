"""One predicate language for every deterministic check.

A pointer walks into a JSON-shaped payload; a comparison judges what it
finds. The project graph's constraints and the actions' postconditions
speak this same small language, so "the wall an object must honor" and
"the state a run promised to produce" are checked by the same code —
deterministic, model-free, and safe by construction: a check NEVER
raises, it just fails.
"""

from __future__ import annotations

from typing import Any

COMPARISONS = ("<=", ">=", "<", ">", "==", "!=", "exists")


def resolve_pointer(payload: dict[str, Any], pointer: str) -> tuple[bool, Any]:
    """``(exists, value)`` for a slash pointer into a nested dict."""
    current: Any = payload
    for part in pointer.strip("/").split("/"):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def pointer_root(pointer: str) -> str:
    return pointer.strip("/").split("/")[0] if pointer.strip("/") else ""


def check(payload: dict[str, Any], pointer: str, op: str, value: Any) -> bool:
    """The verdict for one predicate against one payload. Missing values
    fail every comparison except their absence being the point of
    ``exists``; type mismatches fail rather than raise."""
    exists, found = resolve_pointer(payload, pointer)
    if op == "exists":
        return exists
    if not exists:
        return False
    try:
        if op == "==":
            return bool(found == value)
        if op == "!=":
            return bool(found != value)
        left, right = float(found), float(value)
    except (TypeError, ValueError):
        return False
    if op == "<=":
        return left <= right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == ">":
        return left > right
    return False
