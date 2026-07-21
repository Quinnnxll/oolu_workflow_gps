"""Mainstream languages behind the one sandbox contract.

Exit gate: a drawer whose entry is main.js / main.c / main.cpp / main.sh
runs through a generated Python wrapper that drives the toolchain inside
the same sandbox and speaks emit_result on the program's behalf; main.py
stays the native contract, wrapper-free; and the wrapper passes the same
safety screen every script passes.
"""

from __future__ import annotations

from oolu.gateway.app import _drawer_function
from oolu.nodeplace.screening import screen_script
from oolu.runtime.polyglot import ENTRY_STEPS, polyglot_entry


def test_python_stays_native_and_wins_over_siblings():
    assert polyglot_entry({"main.py": "x", "main.js": "y"}) is None
    fn = _drawer_function({"files": {"main.py": "print(1)", "notes.md": "n"}})
    assert fn["script"] == "print(1)"
    assert fn["files"] == {"notes.md": "n"}


def test_foreign_entries_run_through_the_wrapper():
    for entry in ("main.js", "main.mjs", "main.sh", "main.c", "main.cpp"):
        fn = _drawer_function({"files": {entry: "src", "data.json": "{}"}})
        script = fn["script"]
        assert "emit_result" in script and "emit_error" in script
        assert repr(ENTRY_STEPS[entry]) in script
        # The source stays STAGED — it is the program the wrapper runs.
        assert fn["files"][entry] == "src"
        # The wrapper passes the same safety screen as any script.
        assert screen_script(script) == []


def test_data_and_markup_are_assets_not_entries():
    fn = _drawer_function(
        {"files": {"index.html": "<html/>", "app.jsx": "x", "readme.md": "m"}}
    )
    assert "script" not in fn  # nothing executable: the version JSON stands
    assert set(fn["files"]) == {"index.html", "app.jsx", "readme.md"}
