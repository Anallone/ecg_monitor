@echo off
setlocal
set "ROOT=%~dp0"

rem Pick interpreter: portable runtime first, then local .venv
set "PY=%ROOT%runtime\python\python.exe"
if not exist "%PY%" set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] No Python interpreter found, cannot run pip.
    echo         Copy runtime\ from the work machine first
    echo         ^(see docs\README_DEPLOY.md^).
    pause
    exit /b 1
)

rem All deps are already installed in the portable runtime.
rem This script is only for adding extra packages.
rem Usage: run_pip.bat install some-package
"%PY%" -m pip %*
endlocal
