"""Polyglot node functions: mainstream languages behind one contract.

The sandbox speaks one contract — a Python script that calls
``emit_result`` once — and that stays true. What widens is WHO may be
the function: a drawer whose entry is ``src/main.js``, ``src/main.c``,
``src/main.cpp``, ``src/main.sh`` (or Python, as always) runs through a
generated Python WRAPPER that stages nothing new (the sources are
already staged next to it), invokes the language's toolchain inside the
same sandbox, and speaks the contract on the program's behalf: stdout
becomes the result; a non-zero exit or a missing toolchain is an honest
``emit_error``, never a silent nothing.

Data and markup — JSON, HTML, Markdown, React sources — are not entry
points: they are files a node creates, stages, and ships; the program
that processes them is the function.
"""

from __future__ import annotations

# Entry candidates in preference order, and how each one runs: a list of
# argv steps (compile then run, for the compiled ones).
ENTRY_STEPS: dict[str, list[list[str]]] = {
    "main.py": [],  # native: the file IS the script, no wrapper
    "main.js": [["node", "main.js"]],
    "main.mjs": [["node", "main.mjs"]],
    "main.sh": [["sh", "main.sh"]],
    "main.c": [["cc", "main.c", "-O2", "-o", "main_bin"], ["./main_bin"]],
    "main.cpp": [["c++", "main.cpp", "-O2", "-o", "main_bin"], ["./main_bin"]],
}

ENTRY_ORDER = tuple(ENTRY_STEPS)

# The wrapper's inner subprocess timeout, ALIGNED with the sandbox walls
# (F2): strictly below PROGRAM_LIMITS_MAX.wall_timeout_s (180 s), so under
# the program limits profile the wrapper's honest emit_error fires BEFORE
# the container's wall kill — a timeout with the reason named, never a
# silent kill that loses the message. (Under the tighter step profile the
# 30 s container wall still governs, as it always did — the old inner
# 240 s could never fire first under EITHER profile.)
POLYGLOT_STEP_TIMEOUT_S = 170.0


def polyglot_entry(files: dict[str, str]) -> str | None:
    """The drawer's non-Python entry, if that is what the node is —
    ``main.py`` always wins (the native contract needs no wrapper)."""
    if "main.py" in files:
        return None
    for name in ENTRY_ORDER:
        if name in files:
            return name
    return None


def polyglot_wrapper(entry: str, *, timeout_s: float = POLYGLOT_STEP_TIMEOUT_S) -> str:
    """The Python wrapper that runs a foreign-language entry inside the
    sandbox and speaks the contract for it. ``timeout_s`` bounds each
    toolchain step; the default sits under the program profile's wall so
    a hung toolchain dies with its reason named (see
    ``POLYGLOT_STEP_TIMEOUT_S``)."""
    steps = ENTRY_STEPS[entry]
    return f'''from _oolu_runtime import emit_result, emit_error
import shutil
import subprocess

# Generated wrapper: run the node's own {entry} through its toolchain
# and speak the sandbox contract on its behalf.
steps = {steps!r}
tool = steps[0][0]
if tool != "./main_bin" and shutil.which(tool) is None:
    emit_error(
        f"this node's function is {entry}, but the sandbox has no "
        f"'{{tool}}' toolchain — add it to the runtime image to run "
        "this language"
    )
out = ""
for argv in steps:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout={timeout_s!r}
        )
    except subprocess.TimeoutExpired:
        emit_error(
            f"{{argv[0]}} exceeded {timeout_s:.0f}s — the toolchain hung "
            "or the program runs longer than its sandbox wall"
        )
        raise SystemExit(0)
    if proc.returncode != 0:
        emit_error(
            f"{{argv[0]}} failed (exit {{proc.returncode}}): "
            + (proc.stderr or proc.stdout or "no output")[-2000:]
        )
    out = proc.stdout
emit_result(out.strip() or "done")
'''
