@echo off
rem ============================================================
rem  Common interpreter resolver shared by the Windows helper
rem  scripts. Call it from the project root like this:
rem
rem      call "%~dp0_env.bat"
rem
rem  After the call, ROOT and PY are available in the caller.
rem  PY is empty when neither the portable runtime nor the local
rem  .venv exists. PY_SOURCE is "portable" or "venv".
rem ============================================================
setlocal

set "ROOT=%~dp0"
set "PY="
set "PY_SOURCE="

if exist "%ROOT%runtime\python\python.exe" (
    set "PY=%ROOT%runtime\python\python.exe"
    set "PY_SOURCE=portable"
) else if exist "%ROOT%.venv\Scripts\python.exe" (
    set "PY=%ROOT%.venv\Scripts\python.exe"
    set "PY_SOURCE=venv"
)

rem Keep Python output readable regardless of the Windows code page.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

endlocal & set "ROOT=%ROOT%" & set "PY=%PY%" & set "PY_SOURCE=%PY_SOURCE%" & set "PYTHONUTF8=%PYTHONUTF8%" & set "PYTHONIOENCODING=%PYTHONIOENCODING%"
exit /b 0
