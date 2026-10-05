@echo off
REM ESP-IDF bootstrap (cmd entry).
REM Usage: firmware\idf.bat build   /   firmware\idf.bat -p COMx flash monitor
REM
REM All logic lives in idf.ps1, which auto-detects the ESP-IDF / tools / Python
REM locations, so the same checkout works on both machines without changing any
REM machine-specific environment variables. This file only forwards the call,
REM to avoid maintaining the same paths in two places.
REM
REM Note: comments here are ASCII on purpose -- cmd.exe decodes .bat files using
REM the OEM code page, and non-ASCII comments have broken batch parsing before.
REM The serial port differs per machine/plug (e.g. COM49 on the work machine),
REM so always pass it with -p rather than hard-coding it here.

powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0idf.ps1" %*
exit /b %ERRORLEVEL%
