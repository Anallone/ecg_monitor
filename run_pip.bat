@echo off
setlocal

call "%~dp0_env.bat"

if not defined PY (
    echo [ERROR] No Python interpreter found, cannot run pip.
    echo         Copy runtime\ from the work machine first
    echo         ^(see docs\README_DEPLOY.md^).
    pause
    exit /b 1
)

rem All core deps are already installed in the portable runtime.
rem This script is only for adding packages or installing optional
rem export dependencies:
rem     run_pip.bat install -r requirements-export.txt
"%PY%" -m pip %*
endlocal
