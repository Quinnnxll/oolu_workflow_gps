@echo off
rem =========================================================================
rem  OoLu one-step setup for Windows.
rem  Double-click this file (or run it in a terminal) from the unzipped
rem  repository folder. It will:
rem    1. find Python 3.11+ (and tell you where to get it if missing),
rem    2. create a private virtual environment in .venv (first run only),
rem    3. install OoLu into it,
rem    4. start the desktop shell and open it in your browser.
rem  Nothing is installed outside this folder. Run it again any time —
rem  it reuses the environment and just starts the shell.
rem =========================================================================
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PYTHON=python"
    ) else (
        echo.
        echo   Python was not found on this computer.
        echo   Please install Python 3.11 or newer from https://www.python.org/downloads/
        echo   ^(tick "Add python.exe to PATH" in the installer^), then run this file again.
        echo.
        pause
        exit /b 1
    )
)

%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if not %errorlevel%==0 (
    echo.
    echo   Your Python is too old. OoLu needs Python 3.11 or newer.
    echo   Please install it from https://www.python.org/downloads/ and run this file again.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating a private environment in .venv ...
    %PYTHON% -m venv .venv || goto :fail
)

rem Some Python builds create environments without pip (stripped-down
rem installs, sandboxes). Bootstrap it rather than failing later with
rem "No module named pip".
".venv\Scripts\python.exe" -m pip --version >nul 2>nul
if not %errorlevel%==0 (
    echo This environment is missing pip; bootstrapping it ...
    ".venv\Scripts\python.exe" -m ensurepip --upgrade --default-pip || goto :fail
)

echo Installing OoLu ^(first run can take a few minutes^) ...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto :fail
".venv\Scripts\python.exe" -m pip install --quiet -e ".[serve]" || goto :fail

echo.
echo Starting the OoLu shell ... your browser will open shortly.
echo Keep this window open while you use it; press Ctrl+C here to stop.
echo.
".venv\Scripts\python.exe" -m oolu.cli desktop ^
    --registry .oolu\skills.db --seed-starter --open
exit /b %errorlevel%

:fail
echo.
echo   Something went wrong during setup. The messages above have details.
echo.
pause
exit /b 1
