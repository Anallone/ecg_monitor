@echo off
setlocal enabledelayedexpansion

set "ROOT=%~dp0"
set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"
set "EXPORT=0"

:parse_args
if "%~1"=="" goto :parse_done
if /I "%~1"=="--export" set "EXPORT=1"
if /I "%~1"=="--full" set "EXPORT=1"
if /I "%~1"=="--mirror" (
    set "MIRROR=%~2"
    shift
)
if /I "%~1"=="--help" goto :usage
shift
goto :parse_args

:parse_done
echo ============================================================
echo  Rebuild Python environment locally (fallback, needs network)
echo.
if "%EXPORT%"=="1" (
    echo  Profile: core + TFLite/TensorFlow export chain
) else (
    echo  Profile: core only (training / inference / GUI)
    echo  Add --export to include the optional TFLite/TensorFlow stack.
)
echo  Mirror: %MIRROR%
echo ============================================================
echo.

set "VENV=%ROOT%.venv"
set "PY=%VENV%\Scripts\python.exe"

if exist "%PY%" (
    echo [INFO] Existing .venv found; reusing it.
    echo        To rebuild from scratch, delete the .venv folder first.
    goto :install
)

echo [1/4] Creating Python 3.11 venv .venv ...

where uv >nul 2>&1
if not errorlevel 1 (
    uv venv --python 3.11 --seed "%VENV%"
    if not errorlevel 1 goto :venv_ok
    echo [WARN] uv venv failed; trying py/python fallback.
)

py -3.11 -m venv --copies "%VENV%"
if not errorlevel 1 goto :venv_ok

py -3 -m venv "%VENV%"
if not errorlevel 1 goto :venv_ok

python -m venv "%VENV%"
if not errorlevel 1 goto :venv_ok

echo.
echo [ERROR] Could not create .venv.
echo         Install Python 3.11 or uv, then retry.
echo         https://docs.astral.sh/uv/  or  https://www.python.org/downloads/
goto :fail

:venv_ok
if not exist "%PY%" (
    echo [ERROR] %PY% was not created.
    goto :fail
)

"%PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)"
if errorlevel 1 (
    echo [WARN] The interpreter is not exactly Python 3.11. ML exports are
    echo        best supported on 3.11; you may continue at your own risk.
)

:install
if not exist "%PY%" (
    echo [ERROR] %PY% was not found.
    goto :fail
)

echo.
echo [2/4] Upgrading pip ...
"%PY%" -m pip install --upgrade pip -i "%MIRROR%"
if errorlevel 1 echo [WARN] pip upgrade failed, continuing anyway.

echo.
echo [3/4] Installing core dependencies from requirements.txt ...
"%PY%" -m pip install -r "%ROOT%requirements.txt" -i "%MIRROR%"
if errorlevel 1 (
    echo [ERROR] core dependency install failed.
    goto :fail
)

if "%EXPORT%"=="1" (
    echo.
    echo [3b/4] Installing optional export dependencies ...
    "%PY%" -m pip install -r "%ROOT%requirements-export.txt" -i "%MIRROR%"
    if errorlevel 1 (
        echo [ERROR] export dependency install failed.
        goto :fail
    )
)

echo.
echo [4/4] Verifying core imports ...
"%PY%" -c "import numpy,scipy,matplotlib,sklearn,wfdb,torch,onnx,onnxruntime,PySide6,bleak,PIL;print('    core deps import OK')"
if errorlevel 1 (
    echo [ERROR] Import verification failed even though pip reported success.
    echo         Try running setup again; if it persists, check antivirus/network.
    goto :fail
)

echo.
echo ============================================================
echo  Done. You can now run run_gui.bat / run_demo.bat
if "%EXPORT%"=="0" (
    echo  For TFLite export later:
    echo      run_pip.bat install -r requirements-export.txt
)
echo ============================================================
goto :success

:usage
echo Usage: setup_env_uv.bat [--export] [--full] [--mirror URL]
echo.
echo   --export       install optional TFLite/TensorFlow export dependencies
echo   --full         same as --export
echo   --mirror URL   use a different pip index URL
goto :success

:fail
echo.
echo ============================================================
echo   ENVIRONMENT SETUP FAILED.
echo   Check the messages above, then retry.
echo ============================================================
echo.
pause
exit /b 1

:success
echo.
pause
exit /b 0
