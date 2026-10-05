@echo off
setlocal
set "ROOT=%~dp0"
set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"

echo ============================================================
echo  Rebuild Python environment locally (fallback, needs network)
echo.
echo  Use this ONLY if you cannot copy runtime\ from the work machine.
echo  Creates .venv and installs all deps: ~3-4 GB download, 10-30 min.
echo ============================================================
echo.

where uv >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv not found. Install it first:
    echo         pip install uv    or    https://docs.astral.sh/uv/
    pause
    exit /b 1
)

echo [1/3] Creating Python 3.11 venv .venv ...
uv venv --python 3.11 --seed "%ROOT%.venv"
if errorlevel 1 (
    echo [ERROR] failed to create venv.
    pause
    exit /b 1
)

set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] %PY% was not created.
    pause
    exit /b 1
)

echo.
echo [2/3] Upgrading pip ...
"%PY%" -m pip install --upgrade pip -i %MIRROR%
if errorlevel 1 echo [WARN] pip upgrade failed, continuing anyway.

echo.
echo [3/3] Installing dependencies from Tsinghua mirror ...
"%PY%" -m pip install -r "%ROOT%requirements.txt" -i %MIRROR%
if errorlevel 1 (
    echo.
    echo [ERROR] dependency install failed. Check network and retry,
    echo         or copy runtime\ from the work machine instead.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Done. You can now run run_gui.bat / run_demo.bat
echo ============================================================
pause
endlocal
