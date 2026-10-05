@echo off
setlocal enabledelayedexpansion

call "%~dp0_env.bat"
set "BAD=0"

echo ============================================================
echo  ECG Project - Environment Self-Check
echo  Root: %ROOT%
echo ============================================================
echo.

echo [1] Python interpreter
if defined PY (
    if /I "!PY_SOURCE!"=="portable" (
        echo     [OK]      portable runtime: runtime\python\python.exe
    ) else (
        echo     [OK]      local venv: .venv\Scripts\python.exe
    )
    echo     ==^> will use: !PY!
    "!PY!" --version
    "!PY!" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)"
    if errorlevel 1 (
        echo     [WARN]    this project targets Python 3.11; the version above differs
    )
) else (
    echo     [MISSING] portable runtime: runtime\python\python.exe
    echo     [MISSING] local venv: .venv\Scripts\python.exe
    echo     ==^> No interpreter found. Copy runtime\ from the work machine
    echo         ^(see docs\README_DEPLOY.md^), or run setup_env_uv.bat.
    set /a BAD+=1
)
echo.

echo [2] Project data directories
for %%D in (data processed models export src firmware) do (
    if exist "%ROOT%%%D\" (
        echo     [OK]      %%D\
    ) else (
        echo     [MISSING] %%D\
        set /a BAD+=1
    )
)
echo.

echo [3] Model weights and exported artifacts
if exist "%ROOT%models\res_se_cnn_rr4_best.pt" (
    echo     [OK]      models\res_se_cnn_rr4_best.pt  ^(main model^)
) else (
    echo     [MISSING] models\res_se_cnn_rr4_best.pt  ^(run: run.py train --model res_se_cnn_rr4^)
    set /a BAD+=1
)
if exist "%ROOT%models\ds_cnn_best.pt" (
    echo     [OK]      models\ds_cnn_best.pt  ^(legacy baseline^)
) else (
    echo     [OPTIONAL] models\ds_cnn_best.pt
)
if exist "%ROOT%export\c_model\model_data.h" (
    echo     [OK]      export\c_model\model_data.h  ^(int8 C weights^)
) else (
    echo     [OPTIONAL] export\c_model\model_data.h  ^(firmware deployment^)
)
echo.

echo [4] Core dependency import test
if defined PY (
    "!PY!" -c "import numpy,scipy,matplotlib,sklearn,wfdb,torch,onnx,onnxruntime,PySide6,bleak,PIL;print('    [OK]      core deps import OK')"
    if errorlevel 1 (
        echo     [MISSING] some core deps failed to import
        echo               fix with: run_pip.bat install -r requirements.txt
        set /a BAD+=1
    )
) else (
    echo     [SKIP]    no interpreter
)
echo.

echo [5] Optional TFLite/TensorFlow export chain
if defined PY (
    "!PY!" -c "import importlib.util;print('    [OK]      export deps installed' if importlib.util.find_spec('onnx2tf') else '    [OPTIONAL] export deps not installed')"
    if errorlevel 1 echo     [OPTIONAL] install with: run_pip.bat install -r requirements-export.txt
) else (
    echo     [SKIP]    no interpreter
)
echo.

echo [6] Firmware toolchain (optional, for ESP32-S3 only)
if defined IDF_PATH (
    echo     [OK]      IDF_PATH = !IDF_PATH!
) else (
    echo     [NOT SET] IDF_PATH  ^(only needed to build firmware^)
    if exist "%ROOT%firmware\idf.bat" (
        echo               run firmware\idf.bat to auto-detect ESP-IDF
    )
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
