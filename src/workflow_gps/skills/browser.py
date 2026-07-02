from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import ActionEvent, ExecutionOutcome, ExecutionStatus

_ENV_BROWSER_PATH = "WFGPS_BROWSER_PATH"


def discover_chromium() -> str | None:
    explicit = os.environ.get(_ENV_BROWSER_PATH)
    if explicit and Path(explicit).exists():
        return explicit
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not root:
        return None
    for pattern in ("chromium-*/chrome-linux/chrome", "chromium-*/chrome-*/Chromium*"):
        matches = sorted(Path(root).glob(pattern))
        if matches:
            return str(matches[-1])
    return None


@dataclass(frozen=True)
class BrowserPolicy:
    headless: bool = True
    executable_path: str | None = None
    allow_hosts: frozenset[str] = field(default_factory=frozenset)
    timeout_ms: int = 15_000
    nav_timeout_ms: int = 30_000


def _subst(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str):
        for name, replacement in params.items():
            value = value.replace("{{" + name + "}}", str(replacement))
        return value
    if isinstance(value, list):
        return [_subst(item, params) for item in value]
    if isinstance(value, dict):
        return {key: _subst(item, params) for key, item in value.items()}
    return value


def _host(url: str) -> str | None:
    parsed = urlparse(url)
    return parsed.hostname if parsed.scheme in ("http", "https") else None


def _allowed_hosts_for(
    policy_hosts: frozenset[str], params: dict[str, Any], steps: list
) -> frozenset[str]:
    hosts: set[str] = set(policy_hosts)
    candidates = [params.get("url")]
    candidates += [s.get("url") for s in steps if isinstance(s, dict)]
    for candidate in candidates:
        if isinstance(candidate, str) and (host := _host(candidate)):
            hosts.add(host)
    return frozenset(hosts)


class BrowserActionExecutor:
    name = "browser"

    def __init__(self, *, policy: BrowserPolicy | None = None):
        self._policy = policy or BrowserPolicy()
        self._lock = threading.RLock()
        self._completed: dict[str, ExecutionOutcome] = {}
        self._pw = None
        self._browser = None

    def capabilities(self) -> frozenset[str]:
        return frozenset({"run"})

    def execute(self, action: ActionEvent, *, idempotency_key: str) -> ExecutionOutcome:
        with self._lock:
            if idempotency_key in self._completed:
                return self._completed[idempotency_key]
        if action.adapter != self.name or action.operation != "run":
            return self._finish(
                action,
                idempotency_key,
                ExecutionStatus.BLOCKED,
                "unsupported browser action",
            )

        params = dict(action.parameters)
        steps = _subst(params.get("steps", []), params)
        if not isinstance(steps, list) or not steps:
            return self._finish(
                action, idempotency_key, ExecutionStatus.BLOCKED, "no steps to run"
            )
        allowed = _allowed_hosts_for(self._policy.allow_hosts, params, steps)
        started = datetime.now(UTC)
        try:
            evidence = self._run(steps, allowed)
        except _StepError as exc:
            return self._finish(
                action, idempotency_key, ExecutionStatus.FAILED, str(exc), started
            )
        except Exception as exc:  # noqa: BLE001 - any driver failure is a run failure, not a crash
            return self._finish(
                action,
                idempotency_key,
                ExecutionStatus.FAILED,
                f"browser error: {exc}",
                started,
            )
        return self._finish(
            action, idempotency_key, ExecutionStatus.SUCCEEDED, None, started, evidence
        )

    def cancel(self, idempotency_key: str) -> None:
        return None

    def close(self) -> None:
        with self._lock:
            if self._browser is not None:
                self._browser.close()
                self._browser = None
            if self._pw is not None:
                self._pw.stop()
                self._pw = None

    def _browser_handle(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=self._policy.headless,
                executable_path=self._policy.executable_path or discover_chromium(),
            )
        return self._browser

    def _run(self, steps: list, allowed: frozenset[str]) -> dict[str, Any]:
        with self._lock:
            browser = self._browser_handle()
        context = browser.new_context()
        context.set_default_timeout(self._policy.timeout_ms)

        def _guard(route):
            host = _host(route.request.url)
            if host is None or host in allowed:
                route.continue_()
            else:
                route.abort()

        context.route("**/*", _guard)
        page = context.new_page()
        extracted: dict[str, Any] = {}
        try:
            for step in steps:
                if not isinstance(step, dict):
                    raise _StepError("each step must be an object")
                _dispatch(page, step, extracted, self._policy.nav_timeout_ms)
            final_url = page.url
        finally:
            context.close()
        return {"steps_run": len(steps), "final_url": final_url, "extracted": extracted}

    def _finish(
        self,
        action: ActionEvent,
        idempotency_key: str,
        status: ExecutionStatus,
        error: str | None,
        started: datetime | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> ExecutionOutcome:
        started = started or datetime.now(UTC)
        outcome = ExecutionOutcome(
            idempotency_key=idempotency_key,
            skill_id=action.correlation_id,
            status=status,
            evidence=evidence or {},
            error=error,
            started_at=started,
            completed_at=datetime.now(UTC),
        )
        if status is not ExecutionStatus.FAILED:
            with self._lock:
                self._completed[idempotency_key] = outcome
        return outcome


class _StepError(RuntimeError):
    pass


def _dispatch(page, step: dict, extracted: dict, nav_timeout: int) -> None:
    op = step.get("op")
    selector = step.get("selector")
    if op == "goto":
        page.goto(step["url"], timeout=nav_timeout)
    elif op == "click":
        page.click(selector)
    elif op == "fill":
        page.fill(selector, str(step.get("value", "")))
    elif op == "select_option":
        if "label" in step:
            page.select_option(selector, label=str(step["label"]))
        else:
            page.select_option(selector, str(step.get("value", "")))
    elif op == "wait_for":
        page.wait_for_selector(selector, state=step.get("state", "visible"))
    elif op == "read_text":
        extracted[step.get("name", "text")] = page.inner_text(selector)
    elif op == "read_rows":
        extracted[step.get("name", "rows")] = page.eval_on_selector_all(
            f"{selector} tr",
            "rows => rows.map(r => [...r.querySelectorAll('td,th')].map(c => c.innerText.trim()))",
        )
    elif op == "submit":
        if selector:
            page.click(selector)
        else:
            page.keyboard.press("Enter")
    else:
        raise _StepError(f"unsupported step op: {op}")
