#!/usr/bin/env python3
"""In-container script runner for Workflow-GPS sandboxes.

Baked into the sandbox image at /opt/wfgps/entrypoint.py. The host backend runs
Phase B as:

    python /opt/wfgps/entrypoint.py /sandbox/user_script.py

It makes the result shim importable, runs the synthesized script as ``__main__``,
and turns any uncaught exception into a STRUCTURED error envelope (via the shim's
``report_exception``) so the host classifier receives the exception type and a
trimmed traceback rather than only raw stderr.

PURE STANDARD LIBRARY — it must run in a barebones, network-severed container with
nothing installed. No package-relative imports, no third-party deps.

``WFGPS_SANDBOX_DIR`` lets tests point the runner at a temp directory; in the image
it defaults to /sandbox.
"""

import os
import runpy
import sys

_SANDBOX = os.environ.get("WFGPS_SANDBOX_DIR", "/sandbox")


def main(argv: list[str]) -> int:
    # The shim (_wfgps_runtime.py) and the user script live in the sandbox dir.
    if _SANDBOX not in sys.path:
        sys.path.insert(0, _SANDBOX)

    try:
        import _wfgps_runtime as rt
    except Exception:  # noqa: BLE001 - shim missing => cannot honor the contract at all
        sys.stderr.write("wfgps entrypoint: result shim not found in sandbox dir\n")
        return 3

    if len(argv) < 2:
        rt.emit_error("entrypoint received no script path", kind="EntrypointError")
        return 2

    script_path = argv[1]
    try:
        runpy.run_path(script_path, run_name="__main__")
    except SystemExit as exc:  # respect an explicit exit code from the script
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    except BaseException as exc:  # noqa: BLE001 - report EVERYTHING as a structured error
        rt.report_exception(exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
