"""The node-authoring bench — Phase 0 of the context-harness plan.

Node creation is the platform's most complained-about model seat
(``node.build``): unstable, under-effort, unreliable. Before any phase
re-upholsters that seat (docs/context-harness-plan.md), this bench pins
DOWN what the seat actually does today, with numbers: a fixed suite of
authoring goals runs through the REAL authoring paths — the one-shot
``author_node_function`` gates for a plain ``reply`` model, the
``NodeAuthorAgent`` loop for a tool-calling one — and every authored
function is then verified the way production believes in scripts: by
executing it against the runtime contract (``_oolu_runtime``,
``bindings.json``, the result envelope) and checking the answer.

What the scoreboard reports, per goal and in total:

- built / verified / correct-answer rates, and the FIRST-PASS rate;
- the failure taxonomy: refused, no_script, mocked, truncated,
  bad_interface, missing_dependency, contract_violation, script_error,
  wrong_answer, built_conversation, timeout, transport;
- effort: model calls, prompt/completion tokens, cost, wall seconds —
  sliced per goal from the ``ModelCallMeter``, including the new
  ``finish_reason`` books ("length"/"max_tokens" = the 1024-token
  ceiling caught red-handed).

A scripted incumbent (``scripted_author``) holds the reference FIT
line offline, exactly as the careful engineer does for Level B: it
proves the bench machinery end to end with zero model calls, and the
gate a live model must pass is the same one the incumbent passes.

Two deliberate bench-vs-production differences, both documented
because they are FINDINGS, not oversights:

- The bench verifier stages the goal's ``bindings.json`` before every
  run, including the agent's ``verify_function`` hand. Production's
  ``_author_verifier`` stages NO files, so an honest function that
  reads its declared inputs cannot pass the production verify hand
  today — a Phase 4 item.
- The bench mounts a web exchange whose broker REFUSES every call
  (status 0, reason in ``error``) — the contract the function prompt
  teaches. Production's verify hand mounts no exchange at all, so
  ``http_request`` raises ``WebGrantError`` instead of answering.

Run the live audition (the desktop's own provider stack, real spend):

    ANTHROPIC_API_KEY=sk-... python benchmarks/node_authoring.py
    OPENAI_API_KEY=sk-...    python benchmarks/node_authoring.py
    OOLU_LOCAL_URL=http://localhost:11434/v1 OOLU_LOCAL_MODEL=llama3.2 \\
                             python benchmarks/node_authoring.py

    --tier reasoning     seat the provider's reasoning tier (default fast)
    --max-tokens 8192    lift the router's 1024 ceiling (Phase 1 preview)
    --goals slugify      run only goals whose key contains a substring
    --json               machine-readable report
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:  # runnable as a script from the repo root
    sys.path.insert(0, str(_SRC))

from oolu.author import NodeAuthorAgent  # noqa: E402
from oolu.chat import author_node_function  # noqa: E402
from oolu.runtime import sandbox_shim  # noqa: E402
from oolu.runtime.contract import ContractStatus, parse_stdout  # noqa: E402

BENCH_PURPOSE = "bench.node_authoring"

# The provider words that mean "the completion hit its output ceiling" —
# the exact starvation Phase 1 exists to remove.
TRUNCATION_REASONS = frozenset({"length", "max_tokens"})


# --------------------------------------------------------------------------- #
# The goal suite                                                               #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BenchGoal:
    """One authoring exam question.

    ``kind`` is "build" (a function must be authored and verified) or
    "conversation" (the model must refuse — judgement is half the seat).
    ``bindings`` are staged as ``./bindings.json`` exactly as a run
    stages resolved slot values. ``expect_inputs`` is the interface the
    goal plainly requires — a published node missing one of them cannot
    chain on a route, however well its script runs. ``value_check``
    judges the emitted payload; None means "ports present" suffices.
    ``upstream`` names a bench-catalog node whose recorded outputs the
    author SHOULD consult (route-position goals). ``honest_error_ok``
    accepts a structured ``emit_error`` as a verified outcome — for
    goals whose real data is deliberately unreachable."""

    key: str
    goal: str
    difficulty: str  # easy | medium | hard | judgement
    kind: str = "build"
    bindings: dict[str, str] = field(default_factory=dict)
    expect_inputs: tuple[str, ...] = ()
    expect_outputs: tuple[str, ...] = ("result",)
    value_check: Callable[[dict], bool] | None = None
    upstream: str | None = None
    honest_error_ok: bool = False


def _as_data(value: Any) -> Any:
    """Payload values may arrive as real JSON values or as JSON text —
    both are honest; checks compare the data."""
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except (ValueError, TypeError):
        return value


def _num(value: Any) -> float:
    return float(str(value).strip())


_SALES_ROWS = json.dumps(
    [
        {"sku": "OL-1", "qty": 3, "unit_price": 2.5},
        {"sku": "OL-2", "qty": 2, "unit_price": 4.0},
        {"sku": "OL-3", "qty": 5, "unit_price": 1.2},
    ]
)
_REPORT_ROWS = "date,team,hours\n2026-03-02,atlas,3\n2026-03-02,zephyr,5\n2026-03-03,atlas,4"

# The synthetic desk the agentic author may consult: contracts first
# (the slot vocabulary in circulation), recent run outputs second (the
# ACTUAL shape upstream data arrives in). Mirrors _author_catalog /
# _author_node_outputs shapes in gateway/app.py.
BENCH_CATALOG: list[dict] = [
    {
        "node_id": "node-fetch-sales",
        "title": "Fetch sales rows",
        "goal": "fetch the day's sales rows from the store ledger",
        "consumes": [],
        "produces": [{"name": "sales_rows", "type": "str"}],
    },
    {
        "node_id": "node-fetch-report",
        "title": "Fetch the hours report",
        "goal": "fetch the team hours report",
        "consumes": [],
        "produces": [{"name": "report_rows", "type": "str"}],
    },
    {
        "node_id": "node-notify",
        "title": "Send a notification",
        "goal": "send a short notification message",
        "consumes": [{"name": "message", "type": "str"}],
        "produces": [{"name": "delivery", "type": "str"}],
    },
]

BENCH_OUTPUTS: dict[str, list[dict]] = {
    "node-fetch-sales": [
        {
            "run_id": "r-101",
            "status": "ok",
            "outputs": [{"name": "sales_rows", "value": _SALES_ROWS}],
        }
    ],
    "node-fetch-report": [
        {
            "run_id": "r-207",
            "status": "ok",
            "outputs": [{"name": "report_rows", "value": _REPORT_ROWS}],
        }
    ],
}


def _goals() -> tuple[BenchGoal, ...]:
    return (
        # ------------------------------- easy ------------------------------ #
        BenchGoal(
            key="slugify",
            goal=(
                "Slugify the title input into a lowercase URL slug "
                "(words joined by single hyphens) produced as slug."
            ),
            difficulty="easy",
            bindings={"title": "Hello, World! From OoLu"},
            expect_inputs=("title",),
            expect_outputs=("slug",),
            value_check=lambda p: str(p["slug"]) == "hello-world-from-oolu",
        ),
        BenchGoal(
            key="squares",
            goal=(
                "Square every number in the numbers input (a JSON list) "
                "and produce squares as a JSON list in the same order."
            ),
            difficulty="easy",
            bindings={"numbers": "[1, 2, 3, 10]"},
            expect_inputs=("numbers",),
            expect_outputs=("squares",),
            value_check=lambda p: _as_data(p["squares"]) == [1, 4, 9, 100],
        ),
        BenchGoal(
            key="word-count",
            goal=(
                "Count the words in the text input and produce count "
                "as a number."
            ),
            difficulty="easy",
            bindings={"text": "the quick brown fox jumps"},
            expect_inputs=("text",),
            expect_outputs=("count",),
            value_check=lambda p: int(_num(p["count"])) == 5,
        ),
        BenchGoal(
            key="reverse-lines",
            goal=(
                "Reverse the order of the lines in the text input and "
                "produce reversed_text."
            ),
            difficulty="easy",
            bindings={"text": "alpha\nbeta\ngamma"},
            expect_inputs=("text",),
            expect_outputs=("reversed_text",),
            value_check=lambda p: str(p["reversed_text"]) == "gamma\nbeta\nalpha",
        ),
        BenchGoal(
            key="title-case",
            goal=(
                "Convert the phrase input to title case and produce "
                "title."
            ),
            difficulty="easy",
            bindings={"phrase": "oolu builds nodes"},
            expect_inputs=("phrase",),
            expect_outputs=("title",),
            value_check=lambda p: str(p["title"]) == "Oolu Builds Nodes",
        ),
        BenchGoal(
            key="iso-weekday",
            goal=(
                "Report the ISO weekday number (Monday=1 .. Sunday=7) of "
                "the date input (YYYY-MM-DD) produced as weekday."
            ),
            difficulty="easy",
            bindings={"date": "2026-01-01"},
            expect_inputs=("date",),
            expect_outputs=("weekday",),
            value_check=lambda p: int(_num(p["weekday"])) == 4,
        ),
        BenchGoal(
            key="dedupe",
            goal=(
                "Remove duplicates from the JSON list in the items input, "
                "preserving first-seen order; produce unique as a JSON "
                "list."
            ),
            difficulty="easy",
            bindings={"items": '["a", "b", "a", "c", "b"]'},
            expect_inputs=("items",),
            expect_outputs=("unique",),
            value_check=lambda p: _as_data(p["unique"]) == ["a", "b", "c"],
        ),
        BenchGoal(
            key="sha256",
            goal=(
                "Produce the SHA-256 hex digest of the text input as "
                "digest."
            ),
            difficulty="easy",
            bindings={"text": "oolu"},
            expect_inputs=("text",),
            expect_outputs=("digest",),
            value_check=lambda p: str(p["digest"])
            == __import__("hashlib").sha256(b"oolu").hexdigest(),
        ),
        # ------------------------------ medium ----------------------------- #
        BenchGoal(
            key="csv-total",
            goal=(
                "Sum the amount column of the CSV in the csv input (it has "
                "a header row) and produce total as a number."
            ),
            difficulty="medium",
            bindings={
                "csv": "item,amount\napples,3.50\npears,2.25\nplums,4.25"
            },
            expect_inputs=("csv",),
            expect_outputs=("total",),
            value_check=lambda p: abs(_num(p["total"]) - 10.0) < 1e-9,
        ),
        BenchGoal(
            key="emails",
            goal=(
                "Extract every email address from the text input and "
                "produce emails as a JSON list in order of appearance."
            ),
            difficulty="medium",
            bindings={
                "text": "write ada@example.com or grace@navy.mil today"
            },
            expect_inputs=("text",),
            expect_outputs=("emails",),
            value_check=lambda p: _as_data(p["emails"])
            == ["ada@example.com", "grace@navy.mil"],
        ),
        BenchGoal(
            key="json-pick",
            goal=(
                "From the JSON object in the profile input, produce the "
                "value at the path user.address.city as city."
            ),
            difficulty="medium",
            bindings={
                "profile": '{"user": {"address": {"city": "Taipei"}}}'
            },
            expect_inputs=("profile",),
            expect_outputs=("city",),
            value_check=lambda p: str(p["city"]) == "Taipei",
        ),
        BenchGoal(
            key="log-levels",
            goal=(
                "Count the lines of the log input by level — the word "
                "before the first colon on each line — and produce counts "
                "as a JSON object."
            ),
            difficulty="medium",
            bindings={"log": "INFO: a\nERROR: b\nINFO: c\nWARN: d\nERROR: e"},
            expect_inputs=("log",),
            expect_outputs=("counts",),
            value_check=lambda p: _as_data(p["counts"])
            == {"INFO": 2, "ERROR": 2, "WARN": 1},
        ),
        BenchGoal(
            key="template-fill",
            goal=(
                "Fill the template input (Python str.format placeholders) "
                "with the JSON object in the values input and produce "
                "letter."
            ),
            difficulty="medium",
            bindings={
                "template": "Dear {name}, your order {order} ships {day}.",
                "values": '{"name": "Kai", "order": "A-7", "day": "Friday"}',
            },
            expect_inputs=("template", "values"),
            expect_outputs=("letter",),
            value_check=lambda p: str(p["letter"])
            == "Dear Kai, your order A-7 ships Friday.",
        ),
        BenchGoal(
            key="sort-records",
            goal=(
                "Sort the JSON list in the records input by its score "
                "field, highest first, and produce sorted_names as the "
                "JSON list of the name fields in that order."
            ),
            difficulty="medium",
            bindings={
                "records": (
                    '[{"name": "a", "score": 2}, {"name": "b", "score": 9},'
                    ' {"name": "c", "score": 5}]'
                )
            },
            expect_inputs=("records",),
            expect_outputs=("sorted_names",),
            value_check=lambda p: _as_data(p["sorted_names"]) == ["b", "c", "a"],
        ),
        BenchGoal(
            key="date-span",
            goal=(
                "How many whole days lie between the start and end inputs "
                "(YYYY-MM-DD, end exclusive)? Produce days as a number."
            ),
            difficulty="medium",
            bindings={"start": "2026-02-25", "end": "2026-03-03"},
            expect_inputs=("start", "end"),
            expect_outputs=("days",),
            value_check=lambda p: int(_num(p["days"])) == 6,
        ),
        # ------------------------------- hard ------------------------------ #
        BenchGoal(
            key="top-errors",
            goal=(
                "Each line of the weblog input is 'METHOD /path STATUS'. "
                "Produce top_paths: a JSON list of the 2 paths with the "
                "most 5xx statuses, most frequent first, ties broken "
                "alphabetically."
            ),
            difficulty="hard",
            bindings={
                "weblog": (
                    "GET /a 500\nGET /b 502\nGET /a 503\nPOST /c 200\n"
                    "GET /b 500\nGET /a 200\nPOST /d 501\nGET /b 404"
                )
            },
            expect_inputs=("weblog",),
            expect_outputs=("top_paths",),
            value_check=lambda p: _as_data(p["top_paths"]) == ["/a", "/b"],
        ),
        BenchGoal(
            key="pivot",
            goal=(
                "The sales input is a JSON list of {region, month, units}. "
                "Produce table: a JSON object mapping each region to its "
                "total units."
            ),
            difficulty="hard",
            bindings={
                "sales": (
                    '[{"region": "north", "month": "jan", "units": 3},'
                    ' {"region": "south", "month": "jan", "units": 7},'
                    ' {"region": "north", "month": "feb", "units": 5}]'
                )
            },
            expect_inputs=("sales",),
            expect_outputs=("table",),
            value_check=lambda p: _as_data(p["table"])
            == {"north": 8, "south": 7},
        ),
        BenchGoal(
            key="merge-prefs",
            goal=(
                "Merge the JSON objects in the defaults and overrides "
                "inputs — overrides win, and nested objects merge one "
                "level deep — and produce merged as a JSON object."
            ),
            difficulty="hard",
            bindings={
                "defaults": '{"lang": "en", "page": {"size": 10, "sort": "asc"}}',
                "overrides": '{"page": {"size": 25}, "theme": "dark"}',
            },
            expect_inputs=("defaults", "overrides"),
            expect_outputs=("merged",),
            value_check=lambda p: _as_data(p["merged"])
            == {
                "lang": "en",
                "page": {"size": 25, "sort": "asc"},
                "theme": "dark",
            },
        ),
        BenchGoal(
            key="web-status",
            goal=(
                "Fetch https://api.example.com/status with http_request "
                "and produce the HTTP status number as fetch_status. If "
                "the call is refused, report the refusal honestly with "
                "emit_error naming the reason — never invent a status."
            ),
            difficulty="hard",
            expect_outputs=("fetch_status",),
            honest_error_ok=True,
        ),
        BenchGoal(
            key="route-sales",
            goal=(
                "Total the revenue (qty times unit_price) of the sales "
                "rows the fetch-sales node on this desk produces — they "
                "arrive as the sales_rows input — and produce revenue as "
                "a number."
            ),
            difficulty="hard",
            bindings={"sales_rows": _SALES_ROWS},
            expect_inputs=("sales_rows",),
            expect_outputs=("revenue",),
            upstream="node-fetch-sales",
            value_check=lambda p: abs(_num(p["revenue"]) - 21.5) < 1e-9,
        ),
        BenchGoal(
            key="route-report",
            goal=(
                "From the hours report the fetch-report node on this desk "
                "produces (it arrives as the report_rows input), produce "
                "team_hours: a JSON object mapping each team to its total "
                "hours."
            ),
            difficulty="hard",
            bindings={"report_rows": _REPORT_ROWS},
            expect_inputs=("report_rows",),
            expect_outputs=("team_hours",),
            upstream="node-fetch-report",
            value_check=lambda p: _as_data(p["team_hours"])
            == {"atlas": 7, "zephyr": 5},
        ),
        # ---------------------------- judgement ---------------------------- #
        BenchGoal(
            key="chat-greeting",
            goal="good morning, hope your night went well",
            difficulty="judgement",
            kind="conversation",
        ),
        BenchGoal(
            key="chat-joke",
            goal="tell me a joke about databases",
            difficulty="judgement",
            kind="conversation",
        ),
        BenchGoal(
            key="chat-opinion",
            goal="I think the new design looks great",
            difficulty="judgement",
            kind="conversation",
        ),
    )


GOALS: tuple[BenchGoal, ...] = _goals()


# --------------------------------------------------------------------------- #
# The verifier — the runtime contract, in miniature                            #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VerifyReport:
    """One execution of a candidate function against the contract."""

    ok: bool
    kind: str  # ok | honest_error | contract_violation | script_error |
    #            missing_dependency | timeout
    payload: dict | None = None
    error: str = ""
    healed: tuple[str, ...] = ()


_MISSING_MODULE_RE = re.compile(r"No module named '([^']+)'")

# import name -> distribution name, for the handful that differ. The
# production resolver (runtime/dependency.py) knows more; the bench
# keeps its goals stdlib-only so this map is a safety net, not a crutch.
_IMPORT_TO_PACKAGE = {
    "cv2": "opencv-python",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
}


def _pip_install(package: str, target: Path) -> bool:
    """The default healer: pip-install one package into a scratch dir.
    Returns False (never raises) when the environment cannot install."""
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--disable-pip-version-check",
                "--target",
                str(target),
                package,
            ],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


class BenchScriptRunner:
    """Executes one authored function the way a run would: the REAL
    ``sandbox_shim`` staged as ``_oolu_runtime``, the goal's bindings at
    ``./bindings.json``, a web exchange whose broker refuses every call
    with the reason in ``error`` (status 0), the stdout envelope parsed
    by the REAL ``runtime.contract`` parser, and up to ``max_heals``
    dependency heals through an injectable installer.

    This is a measurement harness, not an isolation boundary: it runs
    with the subprocess backend's trust assumptions (dev/bench only),
    which is exactly the caveat the README pins on that backend."""

    def __init__(
        self,
        *,
        timeout_s: float = 30.0,
        max_heals: int = 2,
        installer: Callable[[str, Path], bool] | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._max_heals = max_heals
        self._installer = installer or _pip_install

    def run(self, script: str, bindings: dict[str, str]) -> VerifyReport:
        with tempfile.TemporaryDirectory(prefix="oolu-authoring-bench-") as raw:
            return self._run_in(Path(raw), script, bindings)

    # ------------------------------------------------------------------ #
    def _run_in(
        self, box: Path, script: str, bindings: dict[str, str]
    ) -> VerifyReport:
        shutil.copyfile(sandbox_shim.__file__, box / "_oolu_runtime.py")
        (box / "script.py").write_text(script, encoding="utf-8")
        if bindings:
            (box / "bindings.json").write_text(
                json.dumps(bindings, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        exchange = box / "web-exchange"
        exchange.mkdir()
        deps = box / "_deps"
        healed: list[str] = []

        stop = threading.Event()
        broker = threading.Thread(
            target=_refusing_broker, args=(exchange, stop), daemon=True
        )
        broker.start()
        try:
            while True:
                report = self._one_pass(box, exchange, deps)
                if report.kind != "missing_dependency":
                    return VerifyReport(
                        ok=report.ok,
                        kind=report.kind,
                        payload=report.payload,
                        error=report.error,
                        healed=tuple(healed),
                    )
                module = report.error
                if len(healed) >= self._max_heals:
                    return VerifyReport(
                        ok=False,
                        kind="missing_dependency",
                        error=(
                            f"missing module {module!r} and the heal "
                            "budget is spent"
                        ),
                        healed=tuple(healed),
                    )
                package = _IMPORT_TO_PACKAGE.get(module, module)
                if not self._installer(package, deps):
                    return VerifyReport(
                        ok=False,
                        kind="missing_dependency",
                        error=(
                            f"missing module {module!r} and {package!r} "
                            "could not be installed"
                        ),
                        healed=tuple(healed),
                    )
                healed.append(package)
        finally:
            stop.set()
            broker.join(timeout=2)

    def _one_pass(self, box: Path, exchange: Path, deps: Path) -> VerifyReport:
        env = dict(os.environ)
        env["OOLU_WEB_EXCHANGE"] = str(exchange)
        if deps.exists():
            env["PYTHONPATH"] = str(deps) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            proc = subprocess.run(
                [sys.executable, "script.py"],
                cwd=box,
                env=env,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
            )
        except subprocess.TimeoutExpired:
            return VerifyReport(
                ok=False,
                kind="timeout",
                error=f"the function ran past {self._timeout_s:.0f}s",
            )
        missing = _MISSING_MODULE_RE.search(proc.stderr or "")
        if missing and proc.returncode != 0:
            # kind carries the module name to the heal loop, which
            # rewrites it into a human sentence on the way out.
            return VerifyReport(
                ok=False, kind="missing_dependency", error=missing.group(1)
            )
        result = parse_stdout(proc.stdout or "")
        if not result.found:
            tail = (proc.stderr or "").strip()[-400:]
            return VerifyReport(
                ok=False,
                kind=(
                    "script_error" if proc.returncode != 0 else "contract_violation"
                ),
                error=(
                    f"exit {proc.returncode}; "
                    f"violation={getattr(result.violation, 'value', None)}; "
                    f"stderr tail: {tail or '(empty)'}"
                ),
            )
        if result.status is ContractStatus.OK:
            return VerifyReport(ok=True, kind="ok", payload=result.payload)
        return VerifyReport(
            ok=False,
            kind="honest_error",
            error=str(result.error_message or "the function reported an error"),
        )


def _refusing_broker(exchange: Path, stop: threading.Event) -> None:
    """Answer every web request with the refusal contract: status 0 and
    the reason in ``error`` — what the prompt teaches functions to read
    and report honestly."""
    suffix = ".req.json"
    while not stop.is_set():
        try:
            requests = [p for p in exchange.iterdir() if p.name.endswith(suffix)]
        except OSError:
            return
        for request_path in requests:
            call_id = request_path.name[: -len(suffix)]
            answer = {
                "status": 0,
                "url": "",
                "content_type": "",
                "body": "",
                "truncated": False,
                "error": (
                    "the authoring bench grants no web hosts — report "
                    "this refusal honestly"
                ),
            }
            try:
                raw = json.loads(request_path.read_text(encoding="utf-8"))
                answer["url"] = str(raw.get("url", ""))
            except (OSError, ValueError):
                pass
            tmp = exchange / (call_id + ".rsp.tmp")
            final = exchange / (call_id + ".rsp.json")
            try:
                tmp.write_text(json.dumps(answer), encoding="utf-8")
                os.replace(tmp, final)
                request_path.unlink(missing_ok=True)
            except OSError:
                pass
        stop.wait(0.02)


# --------------------------------------------------------------------------- #
# Authoring — the real paths, dispatched the way the gateway does              #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Authored:
    script: str | None
    io: dict
    refusal: str
    consultations: int


def author_goal(
    model, goal: BenchGoal, runner: BenchScriptRunner
) -> Authored:
    """Mirror of ``gateway/app.py:_author_function``: a tool-calling
    model works as the ``NodeAuthorAgent`` (bench catalog, recorded
    upstream outputs, and a verify hand that stages the goal's
    bindings); a plain ``reply`` model keeps the one-shot
    ``author_node_function`` gates unchanged."""
    if not hasattr(model, "consult"):
        script, io, refusal = author_node_function(model, goal.goal)
        return Authored(script, io or {}, refusal, consultations=1)

    def verify(script: str) -> dict:
        report = runner.run(script, goal.bindings)
        if report.ok or (goal.honest_error_ok and report.kind == "honest_error"):
            return {"ok": True}
        return {"ok": False, "error": report.error or report.kind}

    agent = NodeAuthorAgent(
        model,
        catalog=lambda: BENCH_CATALOG,
        outputs=lambda node_id: BENCH_OUTPUTS.get(node_id, []),
        verify=verify,
    )
    authored = agent.author(goal.goal)
    return Authored(
        authored.script,
        authored.io or {},
        authored.refusal,
        consultations=max(authored.consultations, 1),
    )


# --------------------------------------------------------------------------- #
# Scoring                                                                      #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GoalResult:
    goal: BenchGoal
    failure_class: str  # "ok" or a taxonomy bucket
    built: bool
    verified: bool
    answer_ok: bool | None  # None = the goal defines no value check
    interface_ok: bool | None  # None = no declared-interface expectation
    first_pass: bool
    refusal: str = ""
    detail: str = ""
    consultations: int = 0
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    truncated: bool = False
    wall_s: float = 0.0
    healed: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.failure_class == "ok"


# Refusal sentences are the one-shot path's only structured signal; the
# substrings below are the repo's own fixed wordings (chat.py), and a
# test pins the mapping so drift is caught, not guessed at.
_REFUSAL_CLASSES = (
    ("only pretends", "mocked"),
    ("no usable function", "no_script"),
    ("reads as conversation", "refused"),
    ("could not be reached", "transport"),
    ("ran out of authoring steps", "no_script"),
)


def _classify_refusal(refusal: str) -> str:
    lowered = refusal.lower()
    for needle, bucket in _REFUSAL_CLASSES:
        if needle in lowered:
            return bucket
    return "no_script"


def _interface_ok(goal: BenchGoal, io: dict) -> bool | None:
    """Did the declared interface include what the goal plainly needs?
    The silent-degradation default ({} inputs, result:str) FAILS a goal
    that requires inputs — that is the bad_interface bucket existing."""
    if not goal.expect_inputs:
        return None
    declared = {
        str(item.get("name", "")).strip()
        for item in (io or {}).get("inputs", [])
        if isinstance(item, dict)
    }
    return all(name in declared for name in goal.expect_inputs)


def score_goal(
    model,
    goal: BenchGoal,
    runner: BenchScriptRunner,
    *,
    meter=None,
    purpose: str = BENCH_PURPOSE,
) -> GoalResult:
    """One goal through authoring + verification + the taxonomy."""
    before = len(meter.charges(purpose)) if meter is not None else 0
    started = time.monotonic()
    authored = author_goal(model, goal, runner)
    wall_s = time.monotonic() - started
    slice_ = meter.charges(purpose)[before:] if meter is not None else []
    calls = len(slice_)
    prompt_tokens = sum(r.prompt_tokens for r in slice_)
    completion_tokens = sum(r.completion_tokens for r in slice_)
    cost = sum(r.cost for r in slice_)
    truncated = any(r.finish_reason in TRUNCATION_REASONS for r in slice_)

    effort = dict(
        consultations=authored.consultations,
        model_calls=calls,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost=cost,
        truncated=truncated,
        wall_s=wall_s,
    )

    if goal.kind == "conversation":
        if authored.script is None:
            return GoalResult(
                goal=goal,
                failure_class="ok",
                built=False,
                verified=False,
                answer_ok=None,
                interface_ok=None,
                first_pass=True,
                refusal=authored.refusal,
                **effort,
            )
        return GoalResult(
            goal=goal,
            failure_class="built_conversation",
            built=True,
            verified=False,
            answer_ok=None,
            interface_ok=None,
            first_pass=False,
            detail="authored a function for plain conversation",
            **effort,
        )

    if authored.script is None:
        bucket = _classify_refusal(authored.refusal)
        if truncated and bucket in ("no_script", "transport"):
            bucket = "truncated"
        return GoalResult(
            goal=goal,
            failure_class=bucket,
            built=False,
            verified=False,
            answer_ok=None,
            interface_ok=None,
            first_pass=False,
            refusal=authored.refusal,
            **effort,
        )

    report = runner.run(authored.script, goal.bindings)
    verified = report.ok or (
        goal.honest_error_ok and report.kind == "honest_error"
    )
    interface_ok = _interface_ok(goal, authored.io)
    answer_ok: bool | None = None
    if verified and goal.value_check is not None and report.payload is not None:
        try:
            answer_ok = bool(goal.value_check(report.payload))
        except Exception:  # noqa: BLE001 - a missing port is a wrong answer
            answer_ok = False
    elif verified and goal.value_check is None and report.payload is not None:
        # No value check: the declared output ports present IS the check
        # (the same bar _answer_gap holds runs to).
        answer_ok = all(name in report.payload for name in goal.expect_outputs)

    if not verified:
        bucket = report.kind if report.kind != "ok" else "script_error"
        if truncated:
            bucket = "truncated"
        if bucket == "honest_error":
            bucket = "script_error"
        return GoalResult(
            goal=goal,
            failure_class=bucket,
            built=True,
            verified=False,
            answer_ok=None,
            interface_ok=interface_ok,
            first_pass=False,
            detail=report.error,
            healed=report.healed,
            **effort,
        )
    if interface_ok is False:
        return GoalResult(
            goal=goal,
            failure_class="bad_interface",
            built=True,
            verified=True,
            answer_ok=answer_ok,
            interface_ok=False,
            first_pass=False,
            detail=(
                "the declared interface misses inputs the goal plainly "
                f"needs: expected {list(goal.expect_inputs)}, declared "
                f"{[i.get('name') for i in authored.io.get('inputs', [])]}"
            ),
            healed=report.healed,
            **effort,
        )
    if answer_ok is False:
        return GoalResult(
            goal=goal,
            failure_class="wrong_answer",
            built=True,
            verified=True,
            answer_ok=False,
            interface_ok=interface_ok,
            first_pass=False,
            detail=f"payload: {json.dumps(report.payload, default=str)[:300]}",
            healed=report.healed,
            **effort,
        )
    return GoalResult(
        goal=goal,
        failure_class="ok",
        built=True,
        verified=True,
        answer_ok=answer_ok,
        interface_ok=interface_ok,
        first_pass=(authored.consultations <= 1),
        healed=report.healed,
        **effort,
    )


@dataclass(frozen=True)
class BenchReport:
    name: str
    results: tuple[GoalResult, ...]

    # ------------------------- aggregate views ------------------------- #
    @property
    def build_results(self) -> tuple[GoalResult, ...]:
        return tuple(r for r in self.results if r.goal.kind == "build")

    @property
    def conversation_results(self) -> tuple[GoalResult, ...]:
        return tuple(r for r in self.results if r.goal.kind == "conversation")

    @property
    def verified_rate(self) -> float:
        build = self.build_results
        return sum(r.verified for r in build) / len(build) if build else 0.0

    @property
    def first_pass_rate(self) -> float:
        build = self.build_results
        return sum(r.ok and r.first_pass for r in build) / len(build) if build else 0.0

    @property
    def answer_rate(self) -> float:
        checked = [r for r in self.build_results if r.answer_ok is not None]
        return sum(r.answer_ok for r in checked) / len(checked) if checked else 0.0

    @property
    def interface_rate(self) -> float:
        judged = [r for r in self.build_results if r.interface_ok is not None]
        return sum(r.interface_ok for r in judged) / len(judged) if judged else 0.0

    @property
    def conversations_declined(self) -> bool:
        return all(r.ok for r in self.conversation_results)

    @property
    def truncations(self) -> int:
        return sum(r.truncated for r in self.results)

    @property
    def total_cost(self) -> float:
        return sum(r.cost for r in self.results)

    @property
    def total_calls(self) -> int:
        return sum(r.model_calls for r in self.results)

    @property
    def total_wall_s(self) -> float:
        return sum(r.wall_s for r in self.results)

    def taxonomy(self) -> Counter:
        return Counter(r.failure_class for r in self.results)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "verified_rate": round(self.verified_rate, 4),
            "first_pass_rate": round(self.first_pass_rate, 4),
            "answer_rate": round(self.answer_rate, 4),
            "interface_rate": round(self.interface_rate, 4),
            "conversations_declined": self.conversations_declined,
            "truncations": self.truncations,
            "model_calls": self.total_calls,
            "cost": round(self.total_cost, 6),
            "wall_s": round(self.total_wall_s, 2),
            "fit": fit_for_the_seat(self),
            "taxonomy": dict(self.taxonomy()),
            "goals": [
                {
                    "key": r.goal.key,
                    "difficulty": r.goal.difficulty,
                    "class": r.failure_class,
                    "verified": r.verified,
                    "answer_ok": r.answer_ok,
                    "interface_ok": r.interface_ok,
                    "first_pass": r.first_pass,
                    "consultations": r.consultations,
                    "model_calls": r.model_calls,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "cost": round(r.cost, 6),
                    "truncated": r.truncated,
                    "wall_s": round(r.wall_s, 2),
                    "healed": list(r.healed),
                    "detail": (r.detail or r.refusal)[:300],
                }
                for r in self.results
            ],
        }


# The standing bar. The scripted incumbent passes it by construction;
# it is the line a seated model must reach before Phase 4 can call node
# creation reliable — deliberately the same shape as Level B's gate.
FIT_VERIFIED_RATE = 0.90
FIT_ANSWER_RATE = 0.80
FIT_INTERFACE_RATE = 0.90


def fit_for_the_seat(report: BenchReport) -> bool:
    return (
        report.verified_rate >= FIT_VERIFIED_RATE
        and report.answer_rate >= FIT_ANSWER_RATE
        and report.interface_rate >= FIT_INTERFACE_RATE
        and report.truncations == 0
        and report.conversations_declined
    )


def run_bench(
    model,
    *,
    name: str = "model",
    goals: tuple[BenchGoal, ...] = GOALS,
    runner: BenchScriptRunner | None = None,
    meter=None,
    purpose: str = BENCH_PURPOSE,
    echo: Callable[[str], None] | None = None,
) -> BenchReport:
    runner = runner or BenchScriptRunner()
    results = []
    for goal in goals:
        result = score_goal(model, goal, runner, meter=meter, purpose=purpose)
        results.append(result)
        if echo is not None:
            mark = "✓" if result.ok else "✗"
            echo(
                f"  {mark} {goal.key:<14} {goal.difficulty:<9} "
                f"{result.failure_class:<18} calls={result.model_calls} "
                f"out={result.completion_tokens} ${result.cost:.4f}"
            )
    return BenchReport(name=name, results=tuple(results))


# --------------------------------------------------------------------------- #
# The scripted incumbent — the bench's careful engineer                        #
# --------------------------------------------------------------------------- #
_PRELUDE = (
    "import json\n"
    "from _oolu_runtime import emit_result, emit_error\n"
    "with open('bindings.json', encoding='utf-8') as fh:\n"
    "    bindings = json.load(fh)\n"
)


def _canned(io_inputs: list[str], io_outputs: list[str], body: str) -> str:
    """A full protocol-true reply: numbered plan, IO line, fenced script."""
    io_line = json.dumps(
        {
            "inputs": [{"name": n, "type": "str"} for n in io_inputs],
            "outputs": [{"name": n, "type": "str"} for n in io_outputs],
        }
    )
    return (
        "1. Read the staged bindings.\n"
        "2. Compute the answer from the real values.\n"
        "3. Emit the result through the contract.\n"
        f"IO: {io_line}\n"
        "```python\n"
        f"{body}"
        "```\n"
    )


_CANNED_REPLIES: dict[str, str] = {
    "slugify": _canned(
        ["title"],
        ["slug"],
        _PRELUDE
        + "import re\n"
        "slug = re.sub(r'[^a-z0-9]+', '-', bindings['title'].lower()).strip('-')\n"
        "emit_result({'slug': slug})\n",
    ),
    "squares": _canned(
        ["numbers"],
        ["squares"],
        _PRELUDE
        + "numbers = json.loads(bindings['numbers'])\n"
        "emit_result({'squares': [n * n for n in numbers]})\n",
    ),
    "word-count": _canned(
        ["text"],
        ["count"],
        _PRELUDE + "emit_result({'count': len(bindings['text'].split())})\n",
    ),
    "reverse-lines": _canned(
        ["text"],
        ["reversed_text"],
        _PRELUDE
        + "lines = bindings['text'].splitlines()\n"
        "emit_result({'reversed_text': '\\n'.join(reversed(lines))})\n",
    ),
    "title-case": _canned(
        ["phrase"],
        ["title"],
        _PRELUDE + "emit_result({'title': bindings['phrase'].title()})\n",
    ),
    "iso-weekday": _canned(
        ["date"],
        ["weekday"],
        _PRELUDE
        + "from datetime import date\n"
        "parts = [int(p) for p in bindings['date'].split('-')]\n"
        "emit_result({'weekday': date(*parts).isoweekday()})\n",
    ),
    "dedupe": _canned(
        ["items"],
        ["unique"],
        _PRELUDE
        + "seen = []\n"
        "for item in json.loads(bindings['items']):\n"
        "    if item not in seen:\n"
        "        seen.append(item)\n"
        "emit_result({'unique': seen})\n",
    ),
    "sha256": _canned(
        ["text"],
        ["digest"],
        _PRELUDE
        + "import hashlib\n"
        "digest = hashlib.sha256(bindings['text'].encode()).hexdigest()\n"
        "emit_result({'digest': digest})\n",
    ),
    "csv-total": _canned(
        ["csv"],
        ["total"],
        _PRELUDE
        + "import csv, io\n"
        "rows = list(csv.DictReader(io.StringIO(bindings['csv'])))\n"
        "emit_result({'total': sum(float(r['amount']) for r in rows)})\n",
    ),
    "emails": _canned(
        ["text"],
        ["emails"],
        _PRELUDE
        + "import re\n"
        "found = re.findall(r'[\\w.+-]+@[\\w-]+\\.[\\w.-]+', bindings['text'])\n"
        "emit_result({'emails': found})\n",
    ),
    "json-pick": _canned(
        ["profile"],
        ["city"],
        _PRELUDE
        + "profile = json.loads(bindings['profile'])\n"
        "emit_result({'city': profile['user']['address']['city']})\n",
    ),
    "log-levels": _canned(
        ["log"],
        ["counts"],
        _PRELUDE
        + "counts = {}\n"
        "for line in bindings['log'].splitlines():\n"
        "    level = line.split(':', 1)[0].strip()\n"
        "    counts[level] = counts.get(level, 0) + 1\n"
        "emit_result({'counts': counts})\n",
    ),
    "template-fill": _canned(
        ["template", "values"],
        ["letter"],
        _PRELUDE
        + "values = json.loads(bindings['values'])\n"
        "emit_result({'letter': bindings['template'].format(**values)})\n",
    ),
    "sort-records": _canned(
        ["records"],
        ["sorted_names"],
        _PRELUDE
        + "records = json.loads(bindings['records'])\n"
        "records.sort(key=lambda r: r['score'], reverse=True)\n"
        "emit_result({'sorted_names': [r['name'] for r in records]})\n",
    ),
    "date-span": _canned(
        ["start", "end"],
        ["days"],
        _PRELUDE
        + "from datetime import date\n"
        "def parse(raw):\n"
        "    return date(*[int(p) for p in raw.split('-')])\n"
        "span = (parse(bindings['end']) - parse(bindings['start'])).days\n"
        "emit_result({'days': span})\n",
    ),
    "top-errors": _canned(
        ["weblog"],
        ["top_paths"],
        _PRELUDE
        + "counts = {}\n"
        "for line in bindings['weblog'].splitlines():\n"
        "    method, path, status = line.split()\n"
        "    if status.startswith('5'):\n"
        "        counts[path] = counts.get(path, 0) + 1\n"
        "ranked = sorted(counts, key=lambda p: (-counts[p], p))\n"
        "emit_result({'top_paths': ranked[:2]})\n",
    ),
    "pivot": _canned(
        ["sales"],
        ["table"],
        _PRELUDE
        + "table = {}\n"
        "for row in json.loads(bindings['sales']):\n"
        "    table[row['region']] = table.get(row['region'], 0) + row['units']\n"
        "emit_result({'table': table})\n",
    ),
    "merge-prefs": _canned(
        ["defaults", "overrides"],
        ["merged"],
        _PRELUDE
        + "merged = json.loads(bindings['defaults'])\n"
        "for key, value in json.loads(bindings['overrides']).items():\n"
        "    if isinstance(value, dict) and isinstance(merged.get(key), dict):\n"
        "        merged[key] = {**merged[key], **value}\n"
        "    else:\n"
        "        merged[key] = value\n"
        "emit_result({'merged': merged})\n",
    ),
    "web-status": _canned(
        [],
        ["fetch_status"],
        "import json\n"
        "from _oolu_runtime import emit_result, emit_error, http_request\n"
        "answer = http_request('https://api.example.com/status')\n"
        "if answer.get('status'):\n"
        "    emit_result({'fetch_status': answer['status']})\n"
        "else:\n"
        "    emit_error('the web call was refused: ' + str(answer.get('error')))\n",
    ),
    "route-sales": _canned(
        ["sales_rows"],
        ["revenue"],
        _PRELUDE
        + "rows = json.loads(bindings['sales_rows'])\n"
        "revenue = sum(r['qty'] * r['unit_price'] for r in rows)\n"
        "emit_result({'revenue': revenue})\n",
    ),
    "route-report": _canned(
        ["report_rows"],
        ["team_hours"],
        _PRELUDE
        + "import csv, io\n"
        "totals = {}\n"
        "for row in csv.DictReader(io.StringIO(bindings['report_rows'])):\n"
        "    totals[row['team']] = totals.get(row['team'], 0) + int(row['hours'])\n"
        "emit_result({'team_hours': totals})\n",
    ),
}


class ScriptedAuthor:
    """The incumbent: a ``reply``-only model that answers every bench
    goal perfectly and refuses every conversation goal — zero model
    calls, the reference the bench machinery is proven against."""

    def reply(self, messages: list[dict]) -> str:
        goal_text = str(messages[-1].get("content", ""))
        for goal in GOALS:
            if goal.goal == goal_text:
                if goal.kind == "conversation":
                    return "NO_TASK"
                return _CANNED_REPLIES[goal.key]
        return "NO_TASK"


def scripted_author() -> ScriptedAuthor:
    return ScriptedAuthor()


# --------------------------------------------------------------------------- #
# The live audition                                                            #
# --------------------------------------------------------------------------- #
def build_brain(workdir: Path, *, tier: str, max_tokens: int | None):
    """The desktop's own model stack, fed from the environment — the
    Level B audition's pattern, seated at the authoring bench. None =
    nothing configured; the caller says so in words."""
    from oolu.billing import ModelCallMeter
    from oolu.durable.connection import DurableConnection
    from oolu.providers.chatmodel import ChatModelRouter
    from oolu.providers.keyring import ModelKeyring

    meter = ModelCallMeter()
    local_url = os.environ.get("OOLU_LOCAL_URL", "").strip()
    local_model = os.environ.get("OOLU_LOCAL_MODEL", "").strip()
    keys = {
        provider: os.environ.get(f"{provider.upper()}_API_KEY", "").strip()
        for provider in ("anthropic", "openai")
    }
    keyed = [provider for provider, key in keys.items() if key]
    if not keyed and not (local_url and local_model):
        return None
    conn = DurableConnection(workdir / "audition.db")
    keyring = ModelKeyring(conn, key_path=workdir / "machine.key")
    for provider in keyed:
        keyring.store("bench", provider, keys[provider])
    kwargs: dict[str, Any] = {}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    router = ChatModelRouter(
        keyring,
        "bench",
        meter=meter,
        tier=lambda: tier,
        source=(lambda: "own-api") if keyed else (lambda: "local"),
        local_url=lambda: local_url,
        local_model=lambda: local_model,
        purpose=BENCH_PURPOSE,
        **kwargs,
    )
    return router, meter


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Node-authoring bench — the node.build seat, measured."
    )
    parser.add_argument("--tier", default="fast", choices=("fast", "reasoning"))
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help=(
            "override the seat profile's output ceiling (default: the "
            "node.build profile's 16384; pass 1024 to recreate the "
            "pre-Phase-1 starvation and watch the truncated bucket fill)"
        ),
    )
    parser.add_argument(
        "--goals", default="", help="run only goals whose key contains this"
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    goals = tuple(g for g in GOALS if args.goals in g.key) or GOALS

    with tempfile.TemporaryDirectory() as workdir:
        brain = build_brain(
            Path(workdir), tier=args.tier, max_tokens=args.max_tokens
        )
        if brain is None:
            print(
                "No brain configured — set ANTHROPIC_API_KEY or "
                "OPENAI_API_KEY (or OOLU_LOCAL_URL + OOLU_LOCAL_MODEL) "
                "and run again. The seat is not pretended into.",
                file=sys.stderr,
            )
            return 2
        router, meter = brain

        if args.max_tokens is not None:
            ceiling = args.max_tokens
        else:
            from oolu.providers.profiles import resolve_profile

            ceiling = resolve_profile(BENCH_PURPOSE).max_tokens
        print(
            f"Node-authoring bench · tier {args.tier} · output ceiling "
            f"{ceiling} tokens · {len(goals)} goals\n"
        )
        incumbent = run_bench(
            scripted_author(), name="scripted-incumbent", goals=goals
        )
        echo = None if args.as_json else print
        challenger = run_bench(
            router,
            name=f"model ({args.tier})",
            goals=goals,
            meter=meter,
            echo=echo,
        )

        if args.as_json:
            print(json.dumps(challenger.as_dict(), indent=2))
            return 0

        print()
        header = (
            f"{'contender':<20} {'verified':>9} {'answers':>8} "
            f"{'interface':>9} {'1st-pass':>9} {'trunc':>6} "
            f"{'calls':>6} {'cost $':>9} {'gate':>8}"
        )
        print(header)
        for report in (incumbent, challenger):
            verdict = "FIT" if fit_for_the_seat(report) else "not fit"
            print(
                f"{report.name:<20} {report.verified_rate:>9.0%} "
                f"{report.answer_rate:>8.0%} {report.interface_rate:>9.0%} "
                f"{report.first_pass_rate:>9.0%} {report.truncations:>6} "
                f"{report.total_calls:>6} {report.total_cost:>9.4f} "
                f"{verdict:>8}"
            )
        print("\nFailure taxonomy (challenger):")
        for bucket, count in sorted(challenger.taxonomy().items()):
            print(f"  {bucket:<20} {count}")
        if fit_for_the_seat(challenger):
            print(
                "\nThe model EARNS the authoring seat at this ceiling — "
                "record the scoreboard and raise the bar in Phase 4."
            )
        else:
            print(
                "\nThe model does NOT earn the authoring seat on this run "
                "— the taxonomy above says which phase of the "
                "context-harness plan pays first."
            )
        return 0


if __name__ == "__main__":
    sys.exit(main())
