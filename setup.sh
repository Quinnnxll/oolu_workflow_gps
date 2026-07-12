#!/usr/bin/env bash
# ===========================================================================
#  OoLu one-step setup for macOS / Linux.
#  Run it from the unzipped repository folder:   ./setup.sh
#  It will:
#    1. find Python 3.11+ (and tell you where to get it if missing),
#    2. create a private virtual environment in .venv (first run only),
#    3. install OoLu into it,
#    4. start the desktop shell and open it in your browser.
#  Nothing is installed outside this folder. Run it again any time —
#  it reuses the environment and just starts the shell.
# ===========================================================================
set -euo pipefail
cd "$(dirname "$0")"

find_python() {
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
                >/dev/null 2>&1; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

if ! PYTHON="$(find_python)"; then
    cat >&2 <<'EOF'

  Python 3.11 or newer was not found on this computer.
  Please install it (https://www.python.org/downloads/ — or your package
  manager, e.g. `brew install python` / `apt install python3`) and run
  this script again.

EOF
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating a private environment in .venv ..."
    "$PYTHON" -m venv .venv
fi

# Some Python builds create environments without pip (Debian/Ubuntu's
# python3-minimal, stripped-down sandboxes). Bootstrap it rather than
# failing later with "No module named pip".
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    echo "This environment is missing pip; bootstrapping it ..."
    .venv/bin/python -m ensurepip --upgrade --default-pip
fi

echo "Installing OoLu (first run can take a few minutes) ..."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[serve]"
# Optional: the CAD geometry hand (heavy download, only if you want it):
#   .venv/bin/python -m pip install -e ".[serve,cad]"

echo
echo "Starting the OoLu shell ... your browser will open shortly."
echo "Keep this terminal open while you use it; press Ctrl+C to stop."
echo
exec .venv/bin/python -m oolu.cli desktop \
    --registry .oolu/skills.db --seed-starter --open
