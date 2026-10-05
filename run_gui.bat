@echo off
setlocal
set "ROOT=%~dp0"

rem Pick interpreter: portable runtime first, then local .venv
set "PY=%ROOT%runtime\python\python.exe"
if not exist "%PY%" set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] No Python interpreter found. Tried:
    echo    1^) %ROOT%runtime\python\python.exe   ^(portable runtime^)
    echo    2^) %ROOT%.venv\Scripts\python.exe      ^(local venv^)
    echo.
    echo On a fresh machine, copy the runtime\ folder from the work machine
    echo ^(about 3.9 GB^) into the project root. See docs\README_DEPLOY.md
    echo or run setup_env_uv.bat to rebuild the environment locally.
    pause
    exit /b 1
)

rem Force UTF-8 for Python output
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

cd /d "%ROOT%src"
"%PY%" gui.py
if errorlevel 1 (
    echo.
    echo [ERROR] GUI exited with code %errorlevel%
    pause
)
endlocal
