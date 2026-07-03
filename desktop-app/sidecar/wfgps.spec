# PyInstaller spec: bundle the wfgps CLI (with the loopback server) into a single
# executable that the Tauri shell runs as a sidecar.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("workflow_gps")
    + ["truststore", "rich", "yaml", "pydantic"]
)
datas = collect_data_files("workflow_gps")

a = Analysis(
    ["wfgps_entry.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["playwright", "docker", "langgraph", "litellm"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="wfgps",
    console=True,
    debug=False,
    strip=False,
    upx=False,
    disable_windowed_traceback=False,
)
