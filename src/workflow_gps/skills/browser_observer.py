from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from .models import ActionEvent

# Installed into every document; reports clicks, field changes, and submits back to
# Python through the `__oolu_record__` binding. Password fields never send a value.
# Idempotent per document (init-script + a direct eval both run it).
_LISTENER_JS = r"""
(() => {
  const sel = (el) => {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return "#" + CSS.escape(el.id);
    const tid = el.getAttribute && el.getAttribute("data-testid");
    if (tid) return '[data-testid="' + tid + '"]';
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node.tagName !== "HTML") {
      if (node.id) { parts.unshift("#" + CSS.escape(node.id)); break; }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (sibs.length > 1) part += ":nth-of-type(" + (sibs.indexOf(node) + 1) + ")";
      }
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(" > ");
  };
  const rec = (payload) => { if (window.__oolu_record__) window.__oolu_record__(payload); };
  const onClick = (e) => {
    const t = (e.target.closest && e.target.closest("button,a,[role=button],input,[onclick]")) || e.target;
    rec({ op: "click", selector: sel(t) });
  };
  const onChange = (e) => {
    const t = e.target;
    if (t.tagName === "SELECT") {
      rec({ op: "select_option", selector: sel(t), value: t.value });
    } else {
      rec({ op: "fill", selector: sel(t), value: (t.type === "password") ? "<MASKED>" : t.value });
    }
  };
  const onSubmit = (e) => rec({ op: "submit", selector: sel(e.target) });
  // Idempotent: set_content keeps `window` but drops document listeners, so we
  // remove any prior handlers (by ref) and re-add — no duplicates, no stale guard.
  const prev = window.__oolu_handlers__;
  if (prev) {
    document.removeEventListener("click", prev.click, true);
    document.removeEventListener("change", prev.change, true);
    document.removeEventListener("submit", prev.submit, true);
  }
  window.__oolu_handlers__ = { click: onClick, change: onChange, submit: onSubmit };
  document.addEventListener("click", onClick, true);
  document.addEventListener("change", onChange, true);
  document.addEventListener("submit", onSubmit, true);
})()
"""


class BrowserObserver:
    """An ``ObserverAdapter`` that records a developer's real browser interactions
    (navigate/click/fill/select/submit) as ``ActionEvent``s in the same vocabulary
    ``BrowserActionExecutor`` replays — so a watched demonstration compiles into a
    re-runnable browser skill. The Playwright ``page`` is injected, so this module
    imports no Playwright; secrets are handled defensively (password values are
    masked at capture, and the learner scrubs the rest downstream).
    """

    name = "browser"

    def __init__(self, *, clock: Callable[[], datetime] | None = None):
        self._clock = clock or (lambda: datetime.now(UTC))
        self._session = uuid4().hex
        self._events: list[ActionEvent] = []
        self._page: Any = None
        self._capabilities = frozenset(
            {"goto", "click", "fill", "select_option", "submit"}
        )

    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def attach(self, page: Any) -> None:
        # The listeners are (re)installed via evaluate on attach and on every
        # `load`; an init script sets the guard flag but its listeners do not fire
        # (isolated world), so we deliberately do not use add_init_script here.
        page.expose_binding("__oolu_record__", self._on_record)
        page.on("load", self._on_load)  # re-install after every navigation
        page.on("framenavigated", self._on_nav)
        self._page = page
        self._install()  # the already-loaded document

    def _install(self) -> None:
        if self._page is None:
            return
        try:
            self._page.evaluate(_LISTENER_JS)
        except Exception:  # noqa: BLE001 - a navigation can invalidate the eval mid-flight
            pass

    def _on_load(self, *_: Any) -> None:
        self._install()

    def observe(self) -> tuple[ActionEvent, ...]:
        return tuple(self._events)

    def clear(self) -> None:
        self._events.clear()

    def _append(self, operation: str, parameters: dict[str, Any]) -> None:
        self._events.append(
            ActionEvent(
                correlation_id=self._session,
                adapter=self.name,
                operation=operation,
                parameters=parameters,
                observed_at=self._clock(),
            )
        )

    def _on_record(self, source: Any, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        op = payload.get("op")
        if op not in self._capabilities:
            return
        parameters: dict[str, Any] = {}
        if payload.get("selector"):
            parameters["selector"] = payload["selector"]
        if payload.get("value") is not None:
            parameters["value"] = payload["value"]
        self._append(op, parameters)

    def _on_nav(self, frame: Any) -> None:
        if self._page is None or frame != self._page.main_frame:
            return
        url = getattr(frame, "url", "") or ""
        if urlparse(url).scheme in ("http", "https", "file"):
            self._append("goto", {"url": url})
