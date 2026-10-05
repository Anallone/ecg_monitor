@echo off
setlocal

call "%~dp0_env.bat"

if not defined PY (
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

rem Default demo: record 200 (contains ventricular beats). Pass args to override.
set "ARGS=--record 200 --model res_se_cnn_rr4"
if not "%~1"=="" set "ARGS=%*"

cd /d "%ROOT%src"
"%PY%" demo.py %ARGS%
if errorlevel 1 echo [ERROR] demo exited with code %errorlevel%
echo.
pause
endlocal
