@echo off
setlocal enabledelayedexpansion
set "ROOT=%~dp0"
set "BAD=0"

echo ============================================================
echo  ECG Project - Environment Self-Check
echo  Root: %ROOT%
echo ============================================================
echo.

echo [1] Python interpreter
set "PY="
if exist "%ROOT%runtime\python\python.exe" (
    set "PY=%ROOT%runtime\python\python.exe"
    echo     [OK]      portable runtime: runtime\python\python.exe
) else (
    echo     [MISSING] portable runtime: runtime\python\python.exe
)
if exist "%ROOT%.venv\Scripts\python.exe" (
    if "!PY!"=="" set "PY=%ROOT%.venv\Scripts\python.exe"
    echo     [OK]      local venv: .venv\Scripts\python.exe
) else (
    echo     [MISSING] local venv: .venv\Scripts\python.exe
)
if "!PY!"=="" (
    echo     ==^> No interpreter found. Copy runtime\ from the work machine
    echo         ^(see docs\README_DEPLOY.md, or run setup_env_uv.bat to rebuild^).
    set /a BAD+=1
) else (
    echo     ==^> will use: !PY!
)
echo.

echo [2] Project data directories
for %%D in (data processed models export report src firmware) do (
    if exist "%ROOT%%%D\" (
        echo     [OK]      %%D\
    ) else (
        echo     [MISSING] %%D\
        set /a BAD+=1
    )
)
echo.

echo [3] Model weights and exported artifacts
if exist "%ROOT%models\ds_cnn_best.pt" (
    echo     [OK]      models\ds_cnn_best.pt
) else (
    echo     [MISSING] models\ds_cnn_best.pt  ^(train first^)
)
if exist "%ROOT%export\c_model\model_data.h" (
    echo     [OK]      export\c_model\model_data.h  ^(int8 weights^)
) else (
    echo     [MISSING] export\c_model\model_data.h
)
echo.

echo [4] Dependency import test
if not "!PY!"=="" (
    "!PY!" -c "import numpy,scipy,pandas,sklearn,wfdb,torch,PySide6;print('    [OK]      core deps import OK (numpy/scipy/pandas/sklearn/wfdb/torch/PySide6)')" 2>nul
    if errorlevel 1 (
        echo     [MISSING] some core deps failed to import
        echo               fix with: run_pip.bat install -r requirements.txt
        set /a BAD+=1
    )
) else (
    echo     [SKIP]    no interpreter
)
echo.

echo [5] Firmware toolchain (optional, for ESP32-S3 only)
if defined IDF_PATH (
    echo     [OK]      IDF_PATH = %IDF_PATH%
) else (
    echo     [NOT SET] IDF_PATH  ^(only needed to build firmware^)
    if exist "D:\esp-idf\" echo               found D:\esp-idf - run its export.bat first
)
echo.

echo ============================================================
if !BAD!==0 (
    echo  RESULT: environment ready - run run_gui.bat / run_demo.bat
) else (
    echo  RESULT: !BAD! item^(s^) missing - see messages above
)
echo ============================================================
pause
endlocal
