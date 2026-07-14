"""The general road, made drivable: a real browser behind the checkout port.

``skills/commerce.py`` defines a ``SiteDriver`` port and a
``SiteDriverExecutor`` that gates the money step behind the payment-consent +
2FA authorization — but no production driver ever implemented the port, so the
general "buy this on any site" road reached nothing. This module fills that
seam with a browser-backed driver, and adds the one capability a real
storefront forces that a headless script cannot fake: **handing the wheel to
the human for login, one-time codes, and CAPTCHAs.**

Three seams, so the whole thing plans and tests without a browser or a network:

- ``BrowserSession`` — the minimal thing the driver needs from a browser:
  run a list of primitive steps in ONE persistent context (so a login
  survives between steps), and answer whether that context is authenticated.
  ``PlaywrightSession`` implements it against a persistent, *headed* Chromium
  profile; a fake satisfies it in tests.
- ``LoginGate`` — the human-control pause. When a step needs an authenticated
  session and the browser is not signed in, the driver stops and asks the
  human to log in (in the real browser they can see), then resumes. The
  default ``AssumeAuthenticated`` never pauses; the desktop shell wires a real
  gate that surfaces the window and blocks until the user is done.
- ``BrowserSiteDriver`` — the ``SiteDriver`` the executor drives. It maps a
  commerce step (``open``/``search``/``add_to_cart``/``checkout``) to the
  browser primitives the plan carries, pausing to the human before any step
  that needs the user's own session.

This driver answers "can we act as the user"; the executor's
``authorization_id`` gate answers "may we spend the user's money." Both must
pass — a signed-in session is not consent to spend, and consent to spend is
not a signed-in session.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, Sequence, runtime_checkable

# The commerce steps that need the user's authenticated session. Browsing and
# searching are anonymous; putting something in a cart or checking out is not.
SESSION_REQUIRED_OPERATIONS = frozenset({"add_to_cart", "checkout"})


class SiteDriverError(RuntimeError):
    """A site step could not be completed — surfaced by the executor as a
    FAILED outcome (never a crash), exactly like any other driver failure."""


class LoginAbandoned(SiteDriverError):
    """The human declined or gave up the login the step required."""


@runtime_checkable
class BrowserSession(Protocol):
    """One persistent browser context the driver runs every step in."""

    def run_steps(self, steps: Sequence[dict]) -> dict:
        """Run browser primitives in order; return extracted evidence.
        Raises on any failed step (the driver turns that into a FAILED
        outcome)."""

    def is_authenticated(self, probe: Any) -> bool:
        """Whether the context is signed in. ``probe`` is site-supplied (a
        selector that is present only when logged in); a falsy probe means
        "cannot tell" and returns True so the gate never fires spuriously."""


@runtime_checkable
class LoginGate(Protocol):
    """The human-control pause for login / 2FA / CAPTCHA."""

    def ensure_authenticated(self, *, site: str, reason: str) -> None:
        """Block until the human has signed the browser in. Raise
        :class:`LoginAbandoned` if they decline. May be a no-op when the
        session is known to be signed in already."""


class AssumeAuthenticated:
    """The default gate: never pauses. For tests and already-signed-in
    profiles, where no human hand-off is wanted."""

    def ensure_authenticated(self, *, site: str, reason: str) -> None:
        return None


class CallbackLoginGate:
    """A gate that hands off to the host UI and blocks until it returns.

    The desktop shell injects ``on_login_required`` — it surfaces the headed
    browser and a "I've signed in" affordance, and returns when the user is
    done (or raises/returns falsy to abandon). Keeping the UI behind a
    callback lets the driver stay UI-agnostic and fully testable.
    """

    def __init__(self, on_login_required: Callable[[str, str], Any]):
        self._on_login_required = on_login_required

    def ensure_authenticated(self, *, site: str, reason: str) -> None:
        outcome = self._on_login_required(site, reason)
        if outcome is False:
            raise LoginAbandoned(f"login to {site} was not completed ({reason})")


class BrowserSiteDriver:
    """Drives a storefront through a :class:`BrowserSession`, pausing to the
    human for any step that needs the user's own session.

    Implements the ``SiteDriver`` port structurally: ``step(operation,
    parameters) -> dict``. Each commerce step carries the browser primitives
    to run under ``parameters["browser_steps"]`` (the site-specific knowledge
    lives in the plan as data, not in this generic driver), and an optional
    ``parameters["login_probe"]`` the session uses to tell whether it is
    signed in.
    """

    def __init__(
        self,
        session: BrowserSession,
        *,
        login_gate: LoginGate | None = None,
        session_required: frozenset[str] = SESSION_REQUIRED_OPERATIONS,
    ):
        self._session = session
        self._login_gate = login_gate or AssumeAuthenticated()
        self._session_required = session_required

    def step(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        site = str(parameters.get("site") or _site_of(parameters) or "the site")
        if self._needs_session(operation, parameters):
            probe = parameters.get("login_probe")
            if not self._session.is_authenticated(probe):
                # Hand the wheel to the human: sign in, clear the 2FA / CAPTCHA,
                # then we resume. We never type the user's password or code.
                self._login_gate.ensure_authenticated(site=site, reason=operation)
                if not self._session.is_authenticated(probe):
                    raise SiteDriverError(
                        f"still not signed in to {site} after the login pause; "
                        f"cannot {operation}"
                    )
        browser_steps = parameters.get("browser_steps") or []
        if not isinstance(browser_steps, list):
            raise SiteDriverError("browser_steps must be a list of step objects")
        evidence = self._session.run_steps(browser_steps)
        return {"operation": operation, "site": site, **evidence}

    def _needs_session(self, operation: str, parameters: dict[str, Any]) -> bool:
        # A step needs the user's session if the operation class requires it,
        # or the plan explicitly flags this step as session-gated.
        return operation in self._session_required or bool(
            parameters.get("requires_session")
        )


def _site_of(parameters: dict[str, Any]) -> str | None:
    from urllib.parse import urlparse

    for step in parameters.get("browser_steps") or []:
        if isinstance(step, dict) and isinstance(step.get("url"), str):
            host = urlparse(step["url"]).hostname
            if host:
                return host
    return None


class BrowserAmazonClient:
    """A session-driven ``AmazonClient`` — the per-site road, honestly.

    Amazon offers no consumer "place my order" API, so the ``amazon`` road
    cannot be a structured one-call adapter the way the port's name suggests.
    This implementation is truthful about that: ``place_order`` drives the
    same persistent browser session as the general road through the cart →
    checkout steps the plan carries, pausing to the human for sign-in / OTP /
    CAPTCHA exactly like ``BrowserSiteDriver`` (which it reuses). It plugs
    ``build_commerce_executors(amazon_client=...)`` unchanged.

    It is a per-site *specialisation*, not a faster protocol: the win over
    the general road is that a plan can carry Amazon-tuned selectors and the
    optimizer can price it as its own road — not that it skips the browser.
    """

    def __init__(
        self,
        session: BrowserSession,
        *,
        login_gate: LoginGate | None = None,
    ):
        # 'order' is session-gated: placing an order always needs the user's
        # signed-in Amazon session, so the login pause fires when needed.
        self._driver = BrowserSiteDriver(
            session,
            login_gate=login_gate,
            session_required=frozenset({"order"}),
        )

    def place_order(self, parameters: dict[str, Any]) -> dict[str, Any]:
        """Drive the browser through Amazon's checkout; return a receipt dict.
        Raises :class:`SiteDriverError` / :class:`LoginAbandoned` on failure,
        which the ``AmazonExecutor`` turns into a FAILED outcome."""
        params = dict(parameters)
        params.setdefault("site", "amazon.com")
        return self._driver.step("order", params)


class PlaywrightSession:  # pragma: no cover - needs Playwright + a display
    """A persistent, headed Chromium profile that survives between steps.

    Unlike ``BrowserActionExecutor`` (which opens a fresh, cookie-less context
    per run), this keeps ONE ``launch_persistent_context`` so a login the human
    performs during a pause is still there for the next step. It is headed by
    default precisely so the human can complete that login. Reuses the browser
    adapter's primitive dispatch (`skills/browser._dispatch`) so the step
    vocabulary is identical to the rest of the engine.
    """

    def __init__(
        self,
        *,
        user_data_dir: str,
        allow_hosts: frozenset[str] = frozenset(),
        headless: bool = False,
        executable_path: str | None = None,
        timeout_ms: int = 15_000,
        nav_timeout_ms: int = 30_000,
    ):
        self._user_data_dir = user_data_dir
        self._allow_hosts = allow_hosts
        self._headless = headless
        self._executable_path = executable_path
        self._timeout_ms = timeout_ms
        self._nav_timeout_ms = nav_timeout_ms
        self._pw = None
        self._context = None
        self._page = None

    def _ensure_page(self):
        from .browser import _host, discover_chromium

        if self._context is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._context = self._pw.chromium.launch_persistent_context(
                self._user_data_dir,
                headless=self._headless,
                executable_path=self._executable_path or discover_chromium(),
            )
            self._context.set_default_timeout(self._timeout_ms)

            def _guard(route):
                host = _host(route.request.url)
                if host is None or not self._allow_hosts or host in self._allow_hosts:
                    route.continue_()
                else:
                    route.abort()

            if self._allow_hosts:
                self._context.route("**/*", _guard)
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
        return self._page

    def run_steps(self, steps: Sequence[dict]) -> dict:
        from .browser import _dispatch, _StepError

        page = self._ensure_page()
        extracted: dict[str, Any] = {}
        for step in steps:
            if not isinstance(step, dict):
                raise SiteDriverError("each browser step must be an object")
            try:
                _dispatch(page, step, extracted, self._nav_timeout_ms)
            except _StepError as exc:
                raise SiteDriverError(str(exc)) from exc
        return {"final_url": page.url, "extracted": extracted}

    def is_authenticated(self, probe: Any) -> bool:
        if not probe:
            return True  # cannot tell -> do not block
        page = self._ensure_page()
        selector = probe if isinstance(probe, str) else probe.get("selector")
        if not selector:
            return True
        try:
            return page.query_selector(selector) is not None
        except Exception:  # noqa: BLE001 - a probe failure is "not signed in"
            return False

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None
