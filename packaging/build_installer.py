"""Build the single-file OoLu shell app for THIS platform.

    python packaging/build_installer.py

PyInstaller cannot cross-compile: run this on Windows for the ``.exe``, on
macOS for the Mac binary, on Linux for the ELF. The GitHub Actions workflow
in ``.github/workflows/build-installers.yml`` does all three per release.
The script is self-sufficient: it installs the project (serve extra) and
PyInstaller into the current environment if they are missing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _ensure(module: str, *pip_args: str) -> None:
    try:
        __import__(module)
    except ImportError:
        print(f"installing {' '.join(pip_args)} ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *pip_args])


def main() -> int:
    _ensure("oolu", "-e", f"{ROOT}[serve]")
    _ensure("PyInstaller", "pyinstaller>=6.0")
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(ROOT / "dist"),
            "--workpath",
            # NOT ROOT/"build": a bare build/ dir at the repo root would
            # shadow the pypa `build` module as a namespace package.
            str(ROOT / ".pyinstaller-work"),
            str(HERE / "oolu_shell.spec"),
        ]
    )
    artifact = next((ROOT / "dist").glob("OoLu-Shell*"))
    print(f"\nBuilt: {artifact}")
    print("Double-click it (or run it) to start the shell.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
