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


def polyglot_entry(files: dict[str, str]) -> str | None:
    """The drawer's non-Python entry, if that is what the node is —
    ``main.py`` always wins (the native contract needs no wrapper)."""
    if "main.py" in files:
        return None
    for name in ENTRY_ORDER:
        if name in files:
            return name
    return None


def polyglot_wrapper(entry: str) -> str:
    """The Python wrapper that runs a foreign-language entry inside the
    sandbox and speaks the contract for it."""
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
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=240)
    if proc.returncode != 0:
        emit_error(
            f"{{argv[0]}} failed (exit {{proc.returncode}}): "
            + (proc.stderr or proc.stdout or "no output")[-2000:]
        )
    out = proc.stdout
emit_result(out.strip() or "done")
'''
