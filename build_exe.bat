@echo off
rem ============================================================
rem  One-click build: PyInstaller one-dir exe for the ECG GUI,
rem  then assemble a portable package (models + data + sdcard)
rem  and zip it. Double-click this file to run everything.
rem
rem  Uses the portable runtime (runtime\python) as the build
rem  environment - all app deps (torch / PySide6 / bleak / wfdb)
rem  are already there, PyInstaller is installed on first run.
rem
rem  This file is pure ASCII with CRLF line endings.
rem
rem  Outputs:
rem    dist\ECGMonitor\ECGMonitor.exe   (the app folder)
rem    ECGMonitor.zip                   (next to this script)
rem  All build output also written to build_exe_log.txt next to this script.
rem ============================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"

set PY=runtime\python\python.exe
set ENTRY=packaging\ecg_monitor_entry.py
set APP_NAME=ECGMonitor
set LOG=build_exe_log.txt

echo ===== Build started =====> "%LOG%"

if not exist "%PY%" (
    echo [ERROR] portable runtime not found: %PY%
    echo [ERROR] portable runtime not found: %PY%>> "%LOG%"
    echo         Copy runtime\ from the work machine first, see docs\README_DEPLOY.md
    goto :fail
)

echo [1/5] Cleaning previous dist folder ...
"%PY%" assemble_package.py --clean-dist >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Clean previous dist folder failed. See %LOG% for details.
    goto :fail
)

echo [2/5] Checking PyInstaller ...
"%PY%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo         PyInstaller missing, installing latest ...
    echo [INFO] Installing PyInstaller ...>> "%LOG%"
    "%PY%" -m pip install -U pyinstaller >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo [ERROR] PyInstaller install failed. See %LOG% for details.
        goto :fail
    )
    echo         Install OK.
)

echo [3/5] Building %APP_NAME% one-dir, no console ...
echo [INFO] Running PyInstaller ...>> "%LOG%"
"%PY%" -m PyInstaller ^
    --noconsole ^
    --onedir ^
    --name %APP_NAME% ^
    --clean ^
    -y ^
    --paths src ^
    --icon packaging\ecg.ico ^
    --add-data "packaging\ecg.ico;packaging" ^
    --hidden-import bleak.backends.winrt.client ^
    --hidden-import bleak.backends.winrt.scanner ^
    --exclude-module matplotlib ^
    --exclude-module onnx ^
    --exclude-module onnxruntime ^
    --exclude-module onnx2tf ^
    --exclude-module onnx_graphsurgeon ^
    --exclude-module tf_keras ^
    --exclude-module ai_edge_litert ^
    --exclude-module sng4onnx ^
    --exclude-module sklearn ^
    --exclude-module torchvision ^
    %ENTRY% >> "%LOG%" 2>&1

if errorlevel 1 (
    echo [ERROR] Build failed. See %LOG% for details.
    goto :fail
)

echo [4/5] Assembling portable package (models + data + sdcard) ...
echo [INFO] Running assemble_package.py ...>> "%LOG%"
"%PY%" assemble_package.py >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Package assembly failed. See %LOG% for details.
    goto :fail
)

echo [5/5] Done.
echo.
echo Package: %~dp0%APP_NAME%.zip
echo Copy the zip to another PC, unzip, and double-click %APP_NAME%\%APP_NAME%.exe
echo.
pause
endlocal
exit /b 0

:fail
echo.
echo ============================================================
echo   BUILD FAILED. Full log saved to: %~dp0%LOG%
echo   Open that file and check the last lines for the error.
echo ============================================================
echo.
pause
endlocal
exit /b 1
