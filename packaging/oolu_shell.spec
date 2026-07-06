# PyInstaller spec for the single-file OoLu shell app.
# Build with:  python packaging/build.py   (installs PyInstaller if needed)
# Output:      dist/OoLu-Shell (.exe on Windows)
#
# What must ride inside the binary and why:
# - uvicorn's submodules: it selects loops/protocols via dynamic import
#   strings PyInstaller's static analysis cannot see;
# - oolu's data files: the starter skill pack is read through
#   importlib.resources at seed time.
from PyInstaller.utils.hooks import collect_data_files

# uvicorn's dynamically imported implementation modules, spelled out
# statically (collect_submodules spawns an isolated interpreter, which some
# hardened build environments block). This is uvicorn's documented set; a
# missing optional backend (uvloop/httptools/websockets) is skipped cleanly.
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]
datas = collect_data_files("oolu")

a = Analysis(
    ["shell_launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Heavy optional stacks the shell never imports: keep the app small.
        "langgraph",
        "litellm",
        "docker",
        "playwright",
        "psycopg",
        # Optional-extra crypto (JWKS RS256/ES256 lives behind the `oidc`
        # extra); the loopback's identity path is pure-Python HS256.
        "cryptography",
        "OpenSSL",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="OoLu-Shell",
    debug=False,
    strip=False,
    upx=False,
    # A console window doubles as the log + the Ctrl+C stop control —
    # honest for a v1; swap to windowed once the shell gets a tray icon.
    console=True,
)
